"""Subagent registry for Claude-style Markdown agent profiles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False


CLAUDE_TOOL_ALIASES: Dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "multiedit": "edit_file",
    "delete": "delete_file",
    "ls": "list_dir",
    "glob": "search_files",
    "grep": "search_files",
    "bash": "run_command",
    "task": "spawn_agent",
    "websearch": "web_search",
    "webfetch": "browser_get_page_text",
}

SUBAGENT_SETTINGS_FILE = Path("data") / "subagents.json"

SUBAGENT_LOCATIONS: Dict[str, Dict[str, Any]] = {
    "project_limebot": {
        "label": "Project (.limebot/agents)",
        "path": ".limebot/agents",
        "writable": True,
    },
    "project_claude": {
        "label": "Project (.claude/agents)",
        "path": ".claude/agents",
        "writable": True,
    },
    "user_limebot": {
        "label": "Personal (~/.limebot/agents)",
        "path": str(Path.home() / ".limebot" / "agents"),
        "writable": True,
    },
    "user_claude": {
        "label": "Personal (~/.claude/agents)",
        "path": str(Path.home() / ".claude" / "agents"),
        "writable": True,
    },
    "builtin": {
        "label": "Built-in",
        "path": "(built-in)",
        "writable": False,
    },
}

LEGACY_LOCATION_ALIASES: Dict[str, str] = {
    "project": "project_claude",
    "user": "user_claude",
}

BUILTIN_SUBAGENTS: List[Dict[str, Any]] = [
    {
        "name": "explorer",
        "description": (
            "Investigate the codebase, identify relevant files and patterns, and "
            "summarize the best implementation path before editing."
        ),
        "prompt": (
            "You are LimeBot's explorer specialist.\n\n"
            "Trace the codebase carefully, find the most relevant files and existing "
            "patterns, and return a concise summary with recommended entry points, "
            "constraints, and risks. Avoid broad edits unless the parent task "
            "explicitly asks for them."
        ),
        "tools": ["read_file", "search_files", "list_dir"],
        "model": "inherit",
        "disallowed_tools": [],
        "max_turns": 8,
        "background": False,
    },
    {
        "name": "reviewer",
        "description": (
            "Review code changes for correctness issues, regressions, unsafe edge "
            "cases, and missing tests."
        ),
        "prompt": (
            "You are LimeBot's reviewer specialist.\n\n"
            "Prioritize correctness bugs, regressions, unsafe assumptions, and test "
            "gaps. Report concrete findings first with file paths and reasons. Keep "
            "the review concise and actionable."
        ),
        "tools": ["read_file", "search_files", "list_dir", "verify_files", "diagnose_files", "run_command"],
        "model": "inherit",
        "disallowed_tools": [],
        "max_turns": 8,
        "background": False,
    },
    {
        "name": "verifier",
        "description": (
            "Verify that a change actually works using targeted tests, builds, and "
            "focused validation commands."
        ),
        "prompt": (
            "You are LimeBot's verifier specialist.\n\n"
            "Assume implementation may be incomplete. Use focused checks and tests to "
            "confirm behavior. Report what passed, what failed, what could not be "
            "verified, and the biggest remaining risk. Do not claim success without "
            "evidence."
        ),
        "tools": ["read_file", "search_files", "list_dir", "verify_files", "diagnose_files", "run_command"],
        "model": "inherit",
        "disallowed_tools": [],
        "max_turns": 8,
        "background": False,
    },
    {
        "name": "researcher",
        "description": (
            "Research a question or topic on the live internet: search the web, "
            "read multiple sources, and report a synthesized, cited answer."
        ),
        "prompt": (
            "You are LimeBot's internet research specialist.\n\n"
            "Investigate the question using web_search and deep_research, then read "
            "the most promising sources with browser_navigate / browser_get_page_text "
            "when you need full page content. Cross-check claims across sources and "
            "prefer primary/authoritative ones. Return a concise answer with inline "
            "[n] citations and a numbered sources list (title + URL). If evidence is "
            "thin or conflicting, say so instead of guessing."
        ),
        "tools": [
            "web_search",
            "image_search",
            "deep_research",
            "browser_navigate",
            "browser_extract",
            "browser_get_page_text",
            "read_file",
            "search_files",
        ],
        "model": "inherit",
        "disallowed_tools": [],
        "max_turns": 10,
        "background": False,
    },
]


def normalize_subagent_tool_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""

    lowered = raw.lower().replace("-", "_")
    return CLAUDE_TOOL_ALIASES.get(lowered, lowered)


def slugify_subagent_filename(name: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name or "").strip()).strip("-_")
    return (text or "subagent").lower()


def _tokenize_for_match(text: str) -> Set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_+-]+", str(text or "").lower())
        if len(token) >= 3
    }


class SubagentRegistry:
    """Discover, manage, and serve named subagent profiles."""

    def __init__(
        self,
        agent_dirs: Optional[List[str]] = None,
        settings_file: Optional[str] = None,
    ):
        self.agent_dirs = agent_dirs or [
            SUBAGENT_LOCATIONS["project_limebot"]["path"],
            SUBAGENT_LOCATIONS["project_claude"]["path"],
            SUBAGENT_LOCATIONS["user_limebot"]["path"],
            SUBAGENT_LOCATIONS["user_claude"]["path"],
        ]
        self.settings_file = (
            Path(settings_file) if settings_file else SUBAGENT_SETTINGS_FILE
        )
        self.subagents: Dict[str, Dict[str, Any]] = {}
        self.default_selection = "auto"
        self._load_settings()

    def _load_settings(self) -> None:
        try:
            if self.settings_file.exists():
                data = json.loads(self.settings_file.read_text(encoding="utf-8"))
                selection = str(data.get("default_selection") or "auto").strip()
                self.default_selection = selection or "auto"
            else:
                self.default_selection = "auto"
        except Exception as exc:
            logger.warning(f"Failed to load subagent settings: {exc}")
            self.default_selection = "auto"

    def _save_settings(self) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"default_selection": self.default_selection}
        self.settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _iter_agent_dirs(self) -> List[tuple[str, Path]]:
        resolved: List[tuple[str, Path]] = []
        for raw_dir in self.agent_dirs:
            agent_dir = Path(raw_dir).expanduser()
            location = self._infer_location_from_path(agent_dir)
            resolved.append((location, agent_dir))
        return resolved

    def _resolve_location_dir(self, location: str) -> Path:
        normalized = self._normalize_location(location)
        meta = SUBAGENT_LOCATIONS[normalized]
        if not meta.get("writable", False):
            raise ValueError("Subagent location is not writable")
        return Path(meta["path"]).expanduser()

    def _normalize_location(self, location: Optional[str]) -> str:
        normalized = str(location or "project_limebot").strip().lower()
        normalized = LEGACY_LOCATION_ALIASES.get(normalized, normalized)
        if normalized not in SUBAGENT_LOCATIONS:
            raise ValueError("Invalid subagent location")
        return normalized

    def _infer_location_from_path(self, agent_dir: Path) -> str:
        try:
            resolved = agent_dir.expanduser().resolve()
        except Exception:
            resolved = agent_dir.expanduser()

        for location, meta in SUBAGENT_LOCATIONS.items():
            if not meta.get("writable", False):
                continue
            candidate = Path(meta["path"]).expanduser()
            try:
                if resolved == candidate.resolve():
                    return location
            except Exception:
                if str(resolved) == str(candidate):
                    return location
        return "project_limebot"

    @staticmethod
    def make_subagent_id(location: str, filename: str) -> str:
        return f"{location}:{filename}"

    @staticmethod
    def parse_subagent_id(subagent_id: str) -> tuple[str, str]:
        raw = str(subagent_id or "").strip()
        if ":" not in raw:
            raise ValueError("Invalid subagent id")
        location, filename = raw.split(":", 1)
        location = LEGACY_LOCATION_ALIASES.get(location, location)
        if location not in SUBAGENT_LOCATIONS:
            raise ValueError("Invalid subagent location")
        if not filename:
            raise ValueError("Invalid subagent filename")
        return location, filename

    def get_location_options(self) -> List[Dict[str, str]]:
        return [
            {"value": key, "label": meta["label"], "path": meta["path"]}
            for key, meta in SUBAGENT_LOCATIONS.items()
            if meta.get("writable", False)
        ]

    def discover_and_load(self) -> Dict[str, Dict[str, Any]]:
        loaded: Dict[str, Dict[str, Any]] = {}

        for location, agent_dir in self._iter_agent_dirs():
            if not agent_dir.exists():
                continue

            for agent_file in sorted(agent_dir.glob("*.md")):
                try:
                    subagent = self._load_subagent(agent_file, location=location)
                except Exception as exc:
                    logger.error(f"Error loading subagent {agent_file}: {exc}")
                    continue

                if not subagent:
                    continue

                name = subagent["name"]
                if name in loaded:
                    logger.debug(
                        f"Skipping duplicate subagent '{name}' from {agent_file}; "
                        "higher-priority definition already loaded."
                    )
                    continue

                loaded[name] = subagent
                logger.debug(f"Loaded subagent: {name} ({agent_file})")

        for builtin in BUILTIN_SUBAGENTS:
            subagent = self._load_builtin_subagent(builtin)
            if subagent["name"] in loaded:
                continue
            loaded[subagent["name"]] = subagent

        self.subagents = loaded
        if (
            self.default_selection != "auto"
            and self.default_selection not in self.subagents
        ):
            self.default_selection = "auto"
            try:
                self._save_settings()
            except Exception as exc:
                logger.warning(f"Failed to reset invalid subagent selection: {exc}")
        logger.info(f"Loaded {len(self.subagents)} subagent(s)")
        return self.subagents

    def get_subagent(self, name: Optional[str]) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        return self.subagents.get(str(name).strip())

    def get_default_selection(self) -> str:
        if self.default_selection == "auto":
            return "auto"
        if self.default_selection not in self.subagents:
            return "auto"
        return self.default_selection

    def set_default_selection(self, selection: Optional[str]) -> str:
        normalized = str(selection or "auto").strip() or "auto"
        if normalized != "auto" and normalized not in self.subagents:
            raise ValueError(f"Unknown subagent '{normalized}'")
        self.default_selection = normalized
        self._save_settings()
        return self.default_selection

    def get_selector_options(self) -> List[Dict[str, Any]]:
        options: List[Dict[str, Any]] = [
            {
                "value": "auto",
                "label": "Auto",
                "description": "Let LimeBot choose tools and subagents on its own.",
                "location": None,
                "builtin": False,
            }
        ]
        for name, agent in sorted(self.subagents.items()):
            options.append(
                {
                    "value": name,
                    "label": name,
                    "description": (agent.get("description") or "").strip(),
                    "location": agent.get("location"),
                    "location_label": SUBAGENT_LOCATIONS[
                        str(agent.get("location") or "builtin")
                    ]["label"],
                    "builtin": bool(agent.get("builtin")),
                }
            )
        return options

    def list_definitions(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        first_seen_by_name: Dict[str, str] = {}

        for location, agent_dir in self._iter_agent_dirs():
            if not agent_dir.exists():
                continue
            for agent_file in sorted(agent_dir.glob("*.md")):
                subagent = self._load_subagent(agent_file, location=location)
                if not subagent:
                    continue
                subagent_id = self.make_subagent_id(location, subagent["filename"])
                existing_active = first_seen_by_name.get(subagent["name"])
                active = existing_active is None
                if active:
                    first_seen_by_name[subagent["name"]] = subagent_id
                entries.append(
                    self._serialize_subagent(
                        subagent, subagent_id, active, existing_active
                    )
                )

        for builtin in BUILTIN_SUBAGENTS:
            subagent = self._load_builtin_subagent(builtin)
            subagent_id = self.make_subagent_id("builtin", subagent["filename"])
            existing_active = first_seen_by_name.get(subagent["name"])
            active = existing_active is None
            if active:
                first_seen_by_name[subagent["name"]] = subagent_id
            entries.append(
                self._serialize_subagent(
                    subagent, subagent_id, active, existing_active
                )
            )

        return entries

    def _serialize_subagent(
        self,
        subagent: Dict[str, Any],
        subagent_id: str,
        active: bool,
        shadowed_by: Optional[str],
    ) -> Dict[str, Any]:
        location = str(subagent.get("location") or "builtin")
        return {
            "id": subagent_id,
            "name": subagent["name"],
            "description": subagent.get("description") or "",
            "prompt": subagent.get("prompt") or "",
            "tools": subagent.get("tools"),
            "disallowed_tools": subagent.get("disallowed_tools") or [],
            "model": subagent.get("model") or "inherit",
            "max_turns": subagent.get("max_turns"),
            "background": bool(subagent.get("background")),
            "filename": subagent["filename"],
            "location": location,
            "location_label": SUBAGENT_LOCATIONS[location]["label"],
            "path": subagent.get("source_path"),
            "active": active,
            "shadowed_by": None if active else shadowed_by,
            "builtin": bool(subagent.get("builtin")),
        }

    def get_agent_choices(self) -> List[str]:
        return sorted(self.subagents)

    def get_agent_descriptions(self) -> Dict[str, str]:
        return {
            name: (agent.get("description") or "").strip()
            for name, agent in sorted(self.subagents.items())
        }

    def recommend_subagent(self, task: str) -> Optional[Tuple[str, str]]:
        text = str(task or "").strip().lower()
        if not text:
            return None

        builtin_keyword_matches = [
            (
                "reviewer",
                (
                    "review",
                    "rate this code",
                    "rate the code",
                    "audit",
                    "find bugs",
                    "regression",
                    "missing tests",
                    "code review",
                ),
                "the request sounds like a review focused on bugs, regressions, or test gaps",
            ),
            (
                "verifier",
                (
                    "verify",
                    "does this work",
                    "is this working",
                    "test this",
                    "run tests",
                    "validate",
                    "check if",
                    "confirm",
                ),
                "the request is asking for proof, validation, or testing",
            ),
            (
                "explorer",
                (
                    "find where",
                    "search the codebase",
                    "trace",
                    "investigate",
                    "understand how",
                    "figure out where",
                    "locate",
                    "entry point",
                ),
                "the request sounds like codebase investigation before editing",
            ),
            (
                "researcher",
                (
                    "research",
                    "look up online",
                    "search the web",
                    "find sources",
                    "what's the latest",
                    "latest news",
                    "compare online",
                    "cite sources",
                ),
                "the request needs live internet research across multiple sources",
            ),
        ]
        for name, keywords, reason in builtin_keyword_matches:
            if name in self.subagents and any(keyword in text for keyword in keywords):
                return name, reason

        task_tokens = _tokenize_for_match(text)
        best_name = None
        best_reason = ""
        best_score = 0.0

        for name, agent in self.subagents.items():
            description = (agent.get("description") or "").strip()
            if not description:
                continue
            description_tokens = _tokenize_for_match(description)
            if not description_tokens:
                continue

            overlap = task_tokens & description_tokens
            if not overlap:
                continue

            score = len(overlap) / max(3, min(len(description_tokens), 12))
            if score > best_score and (len(overlap) >= 2 or score >= 0.5):
                best_name = name
                best_score = score
                sample = ", ".join(sorted(list(overlap))[:4])
                best_reason = f"its description overlaps this task ({sample})"

        if best_name:
            return best_name, best_reason
        return None

    def get_prompt_additions(self, current_message: str = "") -> str:
        if not self.subagents:
            return ""

        from core.media_intent import is_chat_media_delivery, is_image_generation_request

        if is_chat_media_delivery(current_message) and not is_image_generation_request(
            current_message
        ):
            return ""

        lines = [
            "\n## Available Subagents\n",
            "Use `spawn_agent` with specialized subagents when the task matches the agent's description.",
            "Subagents are useful for parallelizable research, isolated reviews, focused verification, and keeping the main context clean.",
            "Do not use them excessively for tiny tasks, and do not duplicate work that a delegated subagent is already doing.",
        ]
        selected = self.get_default_selection()
        if selected != "auto":
            lines.append(
                f"The current global assistant mode is `{selected}`. Prefer routing new work through that specialist unless the user asks otherwise."
            )
        recommendation = self.recommend_subagent(current_message)
        if recommendation:
            lines.append(
                f"For this turn, `{recommendation[0]}` is a strong match because {recommendation[1]}."
            )
        lines.append(
            "When one of these fits well, pass its name in the optional `agent` field instead of doing the whole task yourself:\n"
        )

        for name, agent in sorted(self.subagents.items()):
            description = (agent.get("description") or "").strip() or "No description."
            tools = agent.get("tools")
            if tools is None:
                tool_text = "inherits main tools"
            else:
                tool_text = ", ".join(tools) if tools else "no tools"
            disallowed = agent.get("disallowed_tools") or []
            disallowed_text = (
                f"; disallowed: {', '.join(disallowed)}" if disallowed else ""
            )
            max_turns = agent.get("max_turns")
            turn_text = f"; max_turns: {max_turns}" if isinstance(max_turns, int) else ""
            background_text = "; background by default" if agent.get("background") else ""
            lines.append(
                f"- `{name}`: {description} (tools: {tool_text}{disallowed_text}{turn_text}{background_text})"
            )

        return "\n".join(lines) + "\n"

    def save_subagent(
        self,
        *,
        name: str,
        description: str,
        prompt: str,
        tools: Any = None,
        disallowed_tools: Any = None,
        model: str = "inherit",
        max_turns: Any = None,
        background: Any = False,
        location: str = "project_limebot",
        subagent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        name = str(name or "").strip()
        description = str(description or "").strip()
        prompt = str(prompt or "").strip()
        model = str(model or "inherit").strip() or "inherit"
        parsed_max_turns = self._normalize_max_turns(max_turns)
        parsed_background = self._normalize_background(background)
        if not name:
            raise ValueError("Subagent name is required")
        if not description:
            raise ValueError("Subagent description is required")
        if not prompt:
            raise ValueError("Subagent prompt is required")

        normalized_location = self._normalize_location(location)
        target_dir = self._resolve_location_dir(normalized_location)
        target_dir.mkdir(parents=True, exist_ok=True)

        existing_path: Optional[Path] = None
        if subagent_id:
            existing_location, existing_filename = self.parse_subagent_id(subagent_id)
            if existing_location == "builtin":
                raise ValueError("Built-in subagents cannot be edited directly")
            existing_path = (
                self._resolve_location_dir(existing_location) / f"{existing_filename}.md"
            )
            target_dir = self._resolve_location_dir(normalized_location or existing_location)

        filename = slugify_subagent_filename(name)
        target_path = target_dir / f"{filename}.md"

        if existing_path and existing_path.resolve() != target_path.resolve():
            if target_path.exists():
                raise ValueError(f"Subagent file already exists: {target_path.name}")
            if existing_path.exists():
                existing_path.unlink()

        markdown = self.render_subagent_markdown(
            name=name,
            description=description,
            prompt=prompt,
            tools=tools,
            disallowed_tools=disallowed_tools,
            model=model,
            max_turns=parsed_max_turns,
            background=parsed_background,
        )
        target_path.write_text(markdown, encoding="utf-8")
        saved = self._load_subagent(
            target_path,
            location=self._infer_location_from_path(target_dir),
        )
        if not saved:
            raise ValueError("Failed to load saved subagent")
        self.discover_and_load()
        return saved

    def delete_subagent(self, subagent_id: str) -> None:
        location, filename = self.parse_subagent_id(subagent_id)
        if location == "builtin":
            raise ValueError("Built-in subagents cannot be deleted")
        target_path = self._resolve_location_dir(location) / f"{filename}.md"
        if target_path.exists():
            target_path.unlink()
        self.discover_and_load()

    def render_subagent_markdown(
        self,
        *,
        name: str,
        description: str,
        prompt: str,
        tools: Any = None,
        disallowed_tools: Any = None,
        model: str = "inherit",
        max_turns: Optional[int] = None,
        background: bool = False,
    ) -> str:
        normalized_tools = self._normalize_tools(tools)
        normalized_disallowed_tools = self._normalize_tools(disallowed_tools)
        frontmatter_lines = [
            "---",
            f"name: {json.dumps(str(name).strip())}",
            f"description: {json.dumps(str(description or '').strip())}",
        ]
        if normalized_tools is not None:
            frontmatter_lines.append(f"tools: {json.dumps(normalized_tools)}")
        if normalized_disallowed_tools:
            frontmatter_lines.append(
                f"disallowed_tools: {json.dumps(normalized_disallowed_tools)}"
            )
        if str(model or "").strip():
            frontmatter_lines.append(f"model: {json.dumps(str(model).strip())}")
        if isinstance(max_turns, int):
            frontmatter_lines.append(f"max_turns: {max_turns}")
        if background:
            frontmatter_lines.append("background: true")
        frontmatter_lines.append("---")
        body = str(prompt or "").rstrip() + "\n"
        return "\n".join(frontmatter_lines) + "\n" + body

    def _load_subagent(
        self, agent_file: Path, location: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        content = agent_file.read_text(encoding="utf-8")
        metadata: Dict[str, Any] = {}
        body = content

        if content.startswith("---"):
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
            if match:
                metadata = self._parse_frontmatter(match.group(1))
                body = match.group(2)

        name = str(metadata.get("name") or agent_file.stem).strip()
        if not name:
            logger.warning(f"Skipping subagent with no name: {agent_file}")
            return None

        description = str(metadata.get("description") or "").strip()
        prompt = body.strip()
        tools = self._normalize_tools(metadata.get("tools"))
        disallowed_tools = self._normalize_tools(metadata.get("disallowed_tools")) or []
        model = str(metadata.get("model") or "").strip() or "inherit"
        max_turns = self._normalize_max_turns(metadata.get("max_turns"))
        background = self._normalize_background(metadata.get("background"))

        return {
            "name": name,
            "description": description,
            "prompt": prompt,
            "tools": tools,
            "disallowed_tools": disallowed_tools,
            "model": model,
            "max_turns": max_turns,
            "background": background,
            "filename": agent_file.stem,
            "location": location or "project_limebot",
            "source_path": str(agent_file.resolve()),
            "builtin": False,
        }

    def _load_builtin_subagent(self, definition: Dict[str, Any]) -> Dict[str, Any]:
        subagent = dict(definition)
        name = str(subagent["name"]).strip()
        return {
            "name": name,
            "description": str(subagent.get("description") or "").strip(),
            "prompt": str(subagent.get("prompt") or "").strip(),
            "tools": self._normalize_tools(subagent.get("tools")),
            "disallowed_tools": self._normalize_tools(subagent.get("disallowed_tools"))
            or [],
            "model": str(subagent.get("model") or "inherit").strip() or "inherit",
            "max_turns": self._normalize_max_turns(subagent.get("max_turns")),
            "background": self._normalize_background(subagent.get("background")),
            "filename": slugify_subagent_filename(name),
            "location": "builtin",
            "source_path": "(built-in)",
            "builtin": True,
        }

    def _parse_frontmatter(self, frontmatter: str) -> Dict[str, Any]:
        if HAS_YAML:
            try:
                parsed = yaml.safe_load(frontmatter)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        result: Dict[str, Any] = {}
        for line in frontmatter.splitlines():
            line = line.strip()
            if ":" not in line or line.startswith("{"):
                continue
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
        return result

    def _normalize_tools(self, value: Any) -> Optional[List[str]]:
        if value is None:
            return None

        items: List[str]
        if isinstance(value, list):
            if len(value) == 0:
                return []
            items = [str(item).strip() for item in value]
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.startswith("[") and text.endswith("]"):
                text = text[1:-1]
                if not text.strip():
                    return []
            items = [part.strip().strip("'\"") for part in text.split(",")]

        normalized: List[str] = []
        seen: set[str] = set()
        for item in items:
            name = normalize_subagent_tool_name(item)
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append(name)

        return normalized or []

    @staticmethod
    def _normalize_max_turns(value: Any) -> Optional[int]:
        if value in (None, "", False):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("max_turns must be a positive integer")
        if parsed <= 0:
            raise ValueError("max_turns must be a positive integer")
        return parsed

    @staticmethod
    def _normalize_background(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}
