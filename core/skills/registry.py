"""
Skill Registry - Manages loaded skills and injects into LLM context.
"""

import os
import json
import re
import shutil
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Dict, List, Any, Optional

from loguru import logger
from .loader import SkillLoader
from core.runtime_paths import (
    PROJECT_DIR,
    get_local_skills_dir,
    get_plugin_skill_dirs,
)


_BUNDLED_MANIFEST = Path(__file__).with_name("bundled_manifest.json")


def _load_bundled_skill_names() -> set[str]:
    """Read the explicit manifest used to distinguish shipped skills."""

    try:
        value = json.loads(_BUNDLED_MANIFEST.read_text(encoding="utf-8"))
        names = value.get("skills", []) if isinstance(value, dict) else []
        if isinstance(names, list):
            return {str(name).strip() for name in names if str(name).strip()}
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(f"Unable to read bundled skill manifest: {exc}")
    return set()


_SKILL_INVENTORY_PATTERNS = (
    "what skills do you have",
    "what skill do you have",
    "look at your current skills",
    "current skills",
    "available skills",
    "what are your skills",
    "show skills",
    "list skills",
)


class SkillRegistry:
    """
    Central registry for all loaded skills.
    Provides skill documentation for LLM system prompt injection.
    """

    def __init__(self, skill_dirs: List[str] = None, config: Dict = None):
        """
        Initialize the skill registry.

        Args:
            skill_dirs: Directories to scan for skills
            config: Optional config dict with skill-specific settings
        """
        self.config = config or {}
        self.loader = SkillLoader(skill_dirs)
        self.skills: Dict[str, Dict[str, Any]] = {}
        self._api_handlers: Dict[str, Any] = {}
        self._skill_prompt_cache: Dict[str, str] = {}
        # Monotonic revision used by prompt/tool snapshots.  A capability
        # inventory is only useful when consumers can tell whether their
        # cached view is stale after a reload or enable/disable operation.
        self._capability_revision = 0
        self._configured_skill_names: set[str] = set()

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        """Return whether *path* is inside *root* without string-prefix bugs."""

        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _skill_source_metadata(self, skill: Dict[str, Any]) -> tuple[str, bool]:
        """Classify a discovered skill without exposing its filesystem path."""

        base_dir = Path(str(skill.get("base_dir") or ""))
        name = str(skill.get("name") or "").strip()
        skills_cfg = getattr(self.config, "skills", None)
        installed: Any = getattr(skills_cfg, "installed", {}) if skills_cfg else {}
        if isinstance(self.config, dict):
            installed = (self.config.get("skills") or {}).get("installed", {})
        if isinstance(installed, dict):
            record = installed.get(name) or {}
            if isinstance(record, dict) and str(record.get("source") or "").lower() in {
                "git",
                "github",
                "gitlab",
                "repository",
                "plugin",
            }:
                return "git-managed", False

        local_dir = get_local_skills_dir()
        if self._is_under(base_dir, local_dir):
            return "local", True

        for plugin_dir in get_plugin_skill_dirs():
            if self._is_under(base_dir, plugin_dir):
                return "git-managed", False

        bundled = _load_bundled_skill_names()
        project_skills = PROJECT_DIR / "skills"
        if name in bundled and self._is_under(base_dir, project_skills):
            return "bundled", False

        # A skill supplied through an explicit custom directory, or a legacy
        # folder under the checkout, is user-owned for editing purposes. The
        # editor still applies strict path, symlink, and content validation.
        return "legacy-local", True

    def _annotate_skill_source(self, skill: Dict[str, Any]) -> Dict[str, Any]:
        source_kind, editable = self._skill_source_metadata(skill)
        skill["source_kind"] = source_kind
        skill["editable"] = editable
        return skill

    def discover_and_load(self) -> Dict[str, Dict[str, Any]]:
        """
        Discover all skills and load their API handlers if present.

        Returns:
            Dictionary of loaded skills
        """
        logger.info("Loading skills...")
        self._capability_revision += 1
        self.skills = self.loader.discover_skills()
        for skill in self.skills.values():
            self._annotate_skill_source(skill)
        self._api_handlers.clear()
        self._skill_prompt_cache.clear()
        if hasattr(self, "_cached_prompt_additions"):
            delattr(self, "_cached_prompt_additions")

        for name, skill in self.skills.items():
            if skill.get("has_api"):
                self._load_api_handler(name, skill)

        logger.info(f"Loaded {len(self.skills)} skills")

        enabled_skills = []

        if self.config:
            if hasattr(self.config, "skills"):
                skills_cfg = self.config.skills
                if hasattr(skills_cfg, "enabled"):
                    enabled_skills = skills_cfg.enabled

            elif isinstance(self.config, dict) and "skills" in self.config:
                skills_cfg = self.config["skills"]

                enabled_skills = skills_cfg.get("enabled", [])

        self._configured_skill_names = {
            str(name).strip() for name in enabled_skills if str(name).strip()
        }

        # Capture configuration readiness with the discovery snapshot.  A
        # capability query may run after a temporary environment override (or
        # after a secret has been removed from the process); recomputing this
        # from live process state would make the catalog disagree with the
        # registry revision that was actually loaded.
        for skill in self.skills.values():
            skill["missing_configuration"] = self._missing_configuration(skill)

        for name in self.skills:
            self.skills[name]["active"] = False

        if enabled_skills:
            logger.info(f"Enabled skills: {enabled_skills}")
            for name in enabled_skills:
                if name in self.skills:
                    missing = self._missing_dependencies(self.skills[name])
                    if missing:
                        logger.warning(
                            f"Skill '{name}' disabled because dependencies are missing: "
                            f"{', '.join(missing)}"
                        )
                        self.skills[name]["missing_dependencies"] = missing
                        continue
                    self.skills[name]["active"] = True
        else:
            logger.warning("No skills enabled (default secure state).")

        return self.skills

    @property
    def capability_revision(self) -> int:
        """Return the revision of the discovered skill capability snapshot."""
        return self._capability_revision

    @staticmethod
    def _missing_dependencies(skill: Dict[str, Any]) -> List[str]:
        dependencies = skill.get("dependencies") or {}
        if not isinstance(dependencies, dict):
            return []
        missing: List[str] = []
        python_packages = dependencies.get("python") or []
        if isinstance(python_packages, str):
            python_packages = [python_packages]
        for requirement in python_packages:
            package = re.split(
                r"(?:==|>=|<=|~=|!=|>|<)", str(requirement).strip(), maxsplit=1
            )[0]
            package = package.split("[", 1)[0].strip()
            if not package:
                continue
            try:
                importlib_metadata.version(package)
            except importlib_metadata.PackageNotFoundError:
                missing.append(f"python:{package}")

        binaries = dependencies.get("binaries") or []
        if isinstance(binaries, str):
            binaries = [binaries]
        missing.extend(
            str(binary)
            for binary in binaries
            if str(binary).strip() and shutil.which(str(binary).strip()) is None
        )
        return missing

    def _load_api_handler(self, skill_name: str, skill: Dict) -> None:
        """Load the api.py handler module for a skill."""
        import importlib.util
        from pathlib import Path

        api_path = Path(skill["base_dir"]) / "api.py"
        if not api_path.exists():
            return

        try:
            spec = importlib.util.spec_from_file_location(
                f"skill_api_{skill_name}", api_path
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "handle"):
                    self._api_handlers[skill_name] = module.handle
                    logger.debug(f"    API handler loaded for: {skill_name}")
                elif hasattr(module, "SkillHandler"):
                    self._api_handlers[skill_name] = module.SkillHandler()
                    logger.debug(f"    API handler class loaded for: {skill_name}")
        except SystemExit as e:
            logger.error(
                f"    API handler for {skill_name} exited during import (code={e.code}). Skipping."
            )
        except Exception as e:
            logger.error(f"    Error loading API handler for {skill_name}: {e}")

    def _get_active_skills(self) -> Dict[str, Dict[str, Any]]:
        return {k: v for k, v in self.skills.items() if v.get("active", True)}

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        stop_words = {
            "the",
            "and",
            "for",
            "with",
            "from",
            "are",
            "that",
            "this",
            "into",
            "your",
            "current",
            "currently",
            "have",
            "has",
            "will",
            "right",
            "now",
            "when",
            "using",
            "use",
            "via",
            "all",
            "new",
            "get",
            "skill",
            "skills",
            "available",
            "show",
            "list",
            "tell",
            "what",
            "look",
            "about",
            "connected",
            "correctly",
            "does",
            "need",
            "please",
            "agent",
            "bot",
            "their",
            "them",
            "they",
            "you",
        }
        tokens = {
            tok
            for tok in re.findall(r"[a-z0-9_/-]+", (text or "").lower())
            if len(tok) >= 3 and tok not in stop_words
        }
        expanded = set(tokens)
        for token in tokens:
            expanded.update(part for part in re.split(r"[_/-]+", token) if len(part) >= 3)
        return expanded

    def _skill_aliases(self, name: str, skill: Dict[str, Any]) -> set[str]:
        metadata = skill.get("metadata") or {}
        aliases = self._tokenize(name)

        explicit_aliases = metadata.get("aliases", [])
        if isinstance(explicit_aliases, list):
            aliases.update(self._tokenize(" ".join(str(x) for x in explicit_aliases)))
        elif explicit_aliases:
            aliases.update(self._tokenize(str(explicit_aliases)))

        explicit_keywords = metadata.get("keywords", [])
        if isinstance(explicit_keywords, list):
            aliases.update(self._tokenize(" ".join(str(x) for x in explicit_keywords)))
        elif explicit_keywords:
            aliases.update(self._tokenize(str(explicit_keywords)))

        return aliases

    def _skill_keywords(self, name: str, skill: Dict[str, Any]) -> set[str]:
        metadata = skill.get("metadata") or {}
        docs = "\n".join((skill.get("documentation") or "").splitlines()[:40])
        keywords = self._tokenize(f"{name} {skill.get('description', '')} {docs}")
        explicit = metadata.get("keywords", [])
        if isinstance(explicit, list):
            keywords.update(self._tokenize(" ".join(str(x) for x in explicit)))
        elif explicit:
            keywords.update(self._tokenize(str(explicit)))

        # High-value manual hints for terse user requests.
        hints = {
            "browser": {"web", "website", "search", "browse", "page", "url"},
            "discord": {"discord", "guild", "channel", "server", "embed"},
            "download_image": {"wallpaper", "cdn", "honey-badger"},
            "filesystem": {"file", "folder", "directory", "path", "read", "write"},
            "create-plugin-scaffold": {"plugin", "manifest", "marketplace"},
            "jira": {"jira", "ticket", "issue", "attachment", "attachments"},
            "scrapling": {"scrape", "scraping", "selector", "html", "extract"},
            "whatsapp": {"whatsapp", "jid", "media", "send"},
        }
        keywords.update(hints.get(name, set()))
        return keywords

    @staticmethod
    def _looks_like_skill_inventory_request(text: str) -> bool:
        lowered = (text or "").strip().lower()
        return any(pattern in lowered for pattern in _SKILL_INVENTORY_PATTERNS)

    def _format_active_skill_summary(self, skills: Dict[str, Dict[str, Any]]) -> str:
        if not skills:
            return ""

        sections = ["\n## Active Skills\n"]
        sections.append(
            "These are the skills currently enabled for this session. "
            "If one sounds relevant, follow its manual; it may call native tools or use a `run_command` workflow.\n"
        )
        for name, skill in skills.items():
            description = str(skill.get("description") or "").strip()
            sections.append(f"- `{name}`: {description}")
        sections.append("")
        return "\n".join(sections)

    @staticmethod
    def _normalized_name(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")

    def _format_prompt_additions(self, skills: Dict[str, Dict[str, Any]]) -> str:
        if not skills:
            return ""

        sections = ["\n## Available Skills\n"]
        sections.append(
            "These are skill manuals, not callable tool names. Follow each manual: "
            "it may direct a native tool call or a `run_command` workflow.\n"
        )

        for name, skill in skills.items():
            sections.append(f"### {name}")
            sections.append(f"*{skill['description']}*\n")
            sections.append(skill["documentation"])
            sections.append("")

        return "\n".join(sections)

    @staticmethod
    def _metadata_aliases(skill: Dict[str, Any]) -> list[str]:
        metadata = skill.get("metadata") or {}
        aliases = metadata.get("aliases", [])
        if isinstance(aliases, list):
            return [str(alias).strip() for alias in aliases if str(alias).strip()]
        if aliases:
            alias = str(aliases).strip()
            return [alias] if alias else []
        return []

    def get_system_prompt_additions(self) -> str:
        """
        Generate skill documentation to append to LLM system prompt.
        Cached after first call since skills don't change at runtime.

        Returns:
            Formatted string with all skill documentation
        """
        if hasattr(self, "_cached_prompt_additions"):
            return self._cached_prompt_additions

        active_skills = self._get_active_skills()

        if not active_skills:
            self._cached_prompt_additions = ""
            return ""

        self._cached_prompt_additions = self._format_prompt_additions(active_skills)
        return self._cached_prompt_additions

    def list_active_skill_names(self) -> List[str]:
        return sorted(self._get_active_skills().keys())

    def get_required_tool_names(self, skill_name: Optional[str]) -> List[str]:
        """Return native tools an active skill requires for its workflow."""
        if not skill_name:
            return []
        skill = self._get_active_skills().get(skill_name)
        if not skill:
            return []
        metadata = skill.get("metadata") or {}
        required = metadata.get("required_tools", [])
        if isinstance(required, str):
            required = [required]
        if not isinstance(required, list):
            return []
        return list(
            dict.fromkeys(
                str(name).strip()
                for name in required
                if str(name).strip()
            )
        )

    def get_required_tool_names_for_skills(
        self, skill_names: Optional[List[str]] = None
    ) -> List[str]:
        """Return the ordered union of required tools for several skills.

        Automatic relevance routing used to guarantee required tools only for
        slash-invoked skills.  Keeping this union operation in the registry
        lets every caller apply the same rule to explicit and sticky matches.
        """
        names = skill_names or []
        required: list[str] = []
        for name in names:
            for tool_name in self.get_required_tool_names(name):
                if tool_name not in required:
                    required.append(tool_name)
        return required

    def get_relevant_skill_names(
        self,
        user_text: str,
        max_skills: int = 3,
        sticky_skill_names: Optional[List[str]] = None,
    ) -> List[str]:
        """Resolve active skills for a turn without rendering their manuals.

        ``user_text`` may contain the current utterance plus the previous
        substantive request.  ``sticky_skill_names`` is deliberately additive
        so a terse follow-up such as "yes, that one" cannot erase the active
        task's capability context.
        """
        active_skills = self._get_active_skills()
        if not active_skills:
            return []

        text = (user_text or "").strip()
        sticky = [name for name in (sticky_skill_names or []) if name in active_skills]
        if not text:
            return sticky[:max_skills]

        from core.media_intent import is_chat_media_delivery, is_image_generation_request

        if is_chat_media_delivery(text) and not is_image_generation_request(text):
            # Native web_search(kind=images) plus host attach handles chat photos.
            return sticky[:max_skills]

        if self._looks_like_skill_inventory_request(text):
            return sorted(active_skills)[:max_skills]

        tokens = self._tokenize(text)
        lowered = text.lower()
        normalized_lowered = self._normalized_name(lowered)
        scored: list[tuple[int, str]] = []

        for name, skill in active_skills.items():
            aliases = self._skill_aliases(name, skill)
            keywords = self._skill_keywords(name, skill)
            alias_overlap = len(tokens & aliases)
            keyword_overlap = len(tokens & keywords)
            score = keyword_overlap + alias_overlap * 6

            if self._normalized_name(name) in normalized_lowered:
                score += 50
            if "http" in lowered or "www." in lowered:
                if name in {"browser", "scrapling", "download_image"}:
                    score += 3
            if alias_overlap > 0:
                score += 2
            if score > 0:
                scored.append((score, name))

        scored.sort(key=lambda item: (-item[0], item[1]))
        selected: list[str] = []
        for name in sticky:
            if name not in selected:
                selected.append(name)
        for _, name in scored:
            if name not in selected:
                selected.append(name)
            if len(selected) >= max_skills:
                break
        return selected[:max_skills]

    @staticmethod
    def _capability_description(skill: Dict[str, Any]) -> str:
        description = re.sub(r"\s+", " ", str(skill.get("description") or "")).strip()
        return description[:180] + ("..." if len(description) > 180 else "")

    def _missing_configuration(self, skill: Dict[str, Any]) -> List[str]:
        """Check only explicitly declared credential/configuration keys.

        Skills vary widely, so the registry never guesses that a name such as
        ``jira`` implies a particular secret.  A skill can opt into this
        lifecycle check with ``metadata.required_env`` or
        ``metadata.required_config``; values themselves are never returned.
        """
        metadata = skill.get("metadata") or {}
        required_env = metadata.get("required_env") or metadata.get(
            "required_credentials"
        ) or []
        required_config = metadata.get("required_config") or []
        if isinstance(required_env, str):
            required_env = [required_env]
        if isinstance(required_config, str):
            required_config = [required_config]

        entries: Dict[str, Any] = {}
        if hasattr(self.config, "skills"):
            entries = getattr(self.config.skills, "entries", {}) or {}
        elif isinstance(self.config, dict):
            entries = (self.config.get("skills") or {}).get("entries", {}) or {}
        skill_entries = entries.get(str(skill.get("name") or ""), {})
        if not isinstance(skill_entries, dict):
            skill_entries = {}

        missing: List[str] = []
        for key in required_env:
            env_name = str(key).strip()
            if not env_name:
                continue
            configured = str(os.environ.get(env_name) or "").strip()
            if not configured:
                # The web/config UI may keep a skill credential in its entry
                # map and inject it into the process only at execution time.
                configured = str(
                    skill_entries.get(env_name)
                    or skill_entries.get("apiKey")
                    or ""
                ).strip()
            if not configured:
                missing.append(env_name)
        for key in required_config:
            config_name = str(key).strip()
            if config_name and not str(skill_entries.get(config_name) or "").strip():
                missing.append(f"config:{config_name}")
        return list(dict.fromkeys(missing))

    def get_capability_catalog(self, include_inactive: bool = True) -> List[Dict[str, Any]]:
        """Return a compact, non-secret inventory of discovered skills.

        The catalog intentionally reports lifecycle facts separately.  A
        discovered skill is not necessarily enabled, dependency-ready, or
        connected to its external service.
        """
        catalog: list[Dict[str, Any]] = []
        for name, skill in sorted(self.skills.items()):
            active = bool(skill.get("active", False))
            missing = [str(item) for item in (skill.get("missing_dependencies") or [])]
            missing_configuration = [
                str(item)
                for item in (skill.get("missing_configuration") or [])
            ]
            configured = not missing_configuration
            if missing:
                state = "dependencies_missing"
            elif active and missing_configuration:
                state = "configuration_missing"
            elif active:
                state = "ready"
            elif configured:
                state = "disabled"
            else:
                state = "discovered"
            if not include_inactive and not active:
                continue
            catalog.append(
                {
                    "name": name,
                    "type": "skill",
                    "description": self._capability_description(skill),
                    "aliases": self._metadata_aliases(skill)[:8],
                    "required_tools": self.get_required_tool_names(name),
                    "state": state,
                    "operational_state": state,
                    "discovered": True,
                    "enabled": active,
                    "dependencies_ready": not missing,
                    "configured": configured,
                    "configuration_missing": missing_configuration[:8],
                    "connected": None,
                    "verified": False,
                    "missing_dependencies": missing[:8],
                    "source_kind": str(skill.get("source_kind") or "legacy-local"),
                    "editable": bool(skill.get("editable", False)),
                }
            )
        return catalog

    def search_capabilities(
        self, query: str, include_inactive: bool = True, limit: int = 8
    ) -> List[Dict[str, Any]]:
        """Search the skill inventory using names, aliases, docs, and keywords."""
        catalog = self.get_capability_catalog(include_inactive=include_inactive)
        text = (query or "").strip()
        if not text:
            return catalog[: max(1, min(int(limit), 50))]

        tokens = self._tokenize(text)
        lowered = text.lower()
        scored: list[tuple[int, Dict[str, Any]]] = []
        for item in catalog:
            haystack = self._tokenize(
                " ".join(
                    [
                        str(item.get("name") or ""),
                        str(item.get("description") or ""),
                        " ".join(str(alias) for alias in item.get("aliases") or []),
                    ]
                )
            )
            score = len(tokens & haystack)
            if str(item.get("name") or "").lower() in lowered:
                score += 50
            if score:
                result = dict(item)
                result["match_score"] = score
                scored.append((score, result))
        scored.sort(key=lambda row: (-row[0], str(row[1].get("name") or "")))
        return [item for _, item in scored[: max(1, min(int(limit), 50))]]

    def resolve_active_skill_name(self, requested_name: str) -> Optional[str]:
        requested = str(requested_name or "").strip()
        if not requested:
            return None

        active_skills = self._get_active_skills()
        if not active_skills:
            return None

        exact_matches = [
            name for name in active_skills if name.lower() == requested.lower()
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            return None

        normalized_requested = self._normalized_name(requested)
        matches: list[str] = []
        for name, skill in active_skills.items():
            candidates = {self._normalized_name(name)}
            candidates.update(
                self._normalized_name(alias)
                for alias in self._metadata_aliases(skill)
                if alias
            )
            if normalized_requested in candidates:
                matches.append(name)

        if len(matches) == 1:
            return matches[0]
        return None

    def get_forced_prompt_addition(self, skill_name: str) -> str:
        active_skills = self._get_active_skills()
        skill = active_skills.get(skill_name)
        if not skill:
            return ""

        preface = (
            "\n## Forced Skill\n"
            "The user explicitly invoked this skill with a slash command. "
            "Follow this skill manual for the current turn unless it conflicts "
            "with higher-priority system, safety, or tool-confirmation rules.\n"
        )
        return preface + self._format_prompt_additions({skill_name: skill})

    def get_relevant_prompt_additions(
        self, user_text: str, max_skills: int = 3
    ) -> str:
        """
        Return only the skills that are plausibly relevant to this user turn.
        Falls back to no skill docs instead of dumping every enabled skill.
        """
        active_skills = self._get_active_skills()
        if not active_skills:
            return ""

        text = (user_text or "").strip()
        if not text:
            return ""

        cache_key = f"{text.lower()}::{max_skills}"
        cached = self._skill_prompt_cache.get(cache_key)
        if cached is not None:
            return cached

        if self._looks_like_skill_inventory_request(text):
            additions = self._format_active_skill_summary(active_skills)
            self._skill_prompt_cache[cache_key] = additions
            return additions

        selected_names = self.get_relevant_skill_names(text, max_skills=max_skills)
        selected = {
            name: active_skills[name]
            for name in selected_names
            if name in active_skills
        }
        additions = self._format_prompt_additions(selected)
        self._skill_prompt_cache[cache_key] = additions
        return additions

    def get_skill_env(self, skill_name: str) -> Dict[str, str]:
        """
        Get environment variables for a skill execution.
        Injects API keys and config from the main config.

        Args:
            skill_name: Name of the skill

        Returns:
            Environment dictionary with skill-specific variables
        """
        env = os.environ.copy()

        skill_entries = self.config.get("skills", {}).get("entries", {})
        skill_config = skill_entries.get(skill_name, {})

        if "apiKey" in skill_config:
            env_key = f"{skill_name.upper().replace('-', '_')}_API_KEY"
            env[env_key] = skill_config["apiKey"]

        for key, value in skill_config.items():
            if key != "apiKey" and isinstance(value, str):
                env_key = f"{skill_name.upper().replace('-', '_')}_{key.upper()}"
                env[env_key] = value

        return env

    def get_skill(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """Get a skill by name."""
        return self.skills.get(skill_name)

    def has_api_handler(self, skill_name: str) -> bool:
        """Check if a skill has an API handler."""
        return skill_name in self._api_handlers

    def get_api_handler(self, skill_name: str):
        """Get the API handler for a skill."""
        return self._api_handlers.get(skill_name)

    def reload_skill(self, skill_name: str) -> bool:
        """Reload the complete registry and report whether *skill_name* exists.

        A full snapshot is intentional: edits can create/delete ``api.py`` or
        rename a skill, and a one-file reload otherwise leaves stale handlers,
        prompt entries, or enabled state behind.
        """

        self.discover_and_load()
        return skill_name in self.skills

    def refresh(self) -> Dict[str, Dict[str, Any]]:
        """Explicit alias used by mutation tools after a successful edit."""

        return self.discover_and_load()
