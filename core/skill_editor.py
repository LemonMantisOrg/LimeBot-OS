"""Safe inspection and transactional editing for user-owned LimeBot skills.

The agent may repair a local skill, but the repair surface is deliberately
narrow: only registered user-owned skills are editable, every replacement is
anchored to exact text, and the complete candidate is validated before any
live file is replaced.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.file_edits import EditValidationError, apply_text_edits, unified_text_diff
from core.redaction import redact_sensitive_text
from core.runtime_paths import PROJECT_DIR, get_state_dir

try:
    import yaml

    _HAS_YAML = True
except ImportError:  # pragma: no cover - the fallback is covered instead
    yaml = None
    _HAS_YAML = False


MAX_INSPECT_CHARS = 8_000
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_EDIT_BYTES = 10 * 1024 * 1024
MAX_CHANGE_OPERATIONS = 64
MAX_INVENTORY_FILES = 128

_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "limebot.json",
        "package-lock.json",
        "config.py",
        "secrets.py",
        "credentials.json",
        "credentials.txt",
        "id_rsa",
        "authorized_keys",
    }
)
_SENSITIVE_DIRS = frozenset({".git", "node_modules", "__pycache__"})
_SENSITIVE_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx"})
_SECRET_ENV_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "APIKEY",
    "AUTHORIZATION",
    "PRIVATE_KEY",
    "CREDENTIAL",
)


class SkillEditError(ValueError):
    """A redacted, user-actionable skill editing failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _posix_relative(path: Path) -> str:
    return path.as_posix().lstrip("./")


def _safe_secret_values() -> list[str]:
    values: list[str] = []
    for key, value in os.environ.items():
        normalized_key = str(key).upper().replace("-", "_")
        if not any(marker in normalized_key for marker in _SECRET_ENV_MARKERS):
            continue
        text = str(value or "").strip()
        if len(text) >= 8 and text not in values:
            values.append(text)
    return values


def _contains_known_secret(text: str) -> bool:
    return any(secret in text for secret in _safe_secret_values())


def _safe_text(text: str, root: Path) -> str:
    """Redact credentials and runtime roots before returning skill content."""

    safe = str(text)
    roots = [root, PROJECT_DIR, get_state_dir()]
    for candidate in roots:
        for rendered in {str(candidate), str(candidate).replace("\\", "/")}:
            if rendered:
                safe = safe.replace(rendered, "<skill-root>")
    for secret in _safe_secret_values():
        safe = safe.replace(secret, "[REDACTED]")
    return redact_sensitive_text(safe)


def _sensitive_component(component: str) -> bool:
    lowered = component.lower()
    return (
        lowered in _SENSITIVE_NAMES
        or lowered in _SENSITIVE_DIRS
        or lowered.startswith(".env")
        or Path(lowered).suffix in _SENSITIVE_EXTENSIONS
    )


class SkillEditor:
    """Inspect and edit skills owned by the local runtime."""

    def __init__(self, registry: Any, agent: Any = None):
        self.registry = registry
        self.agent = agent

    def _skill_context(
        self, skill_name: str, *, require_editable: bool = True
    ) -> tuple[Dict[str, Any], Path]:
        name = str(skill_name or "").strip()
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise SkillEditError("skill_not_found", "The requested skill is not registered.")
        skill = self.registry.get_skill(name) if self.registry is not None else None
        if not isinstance(skill, dict):
            raise SkillEditError("skill_not_found", "The requested skill is not registered.")
        source_kind = str(skill.get("source_kind") or "legacy-local")
        if require_editable and (not bool(skill.get("editable")) or source_kind not in {
            "local",
            "legacy-local",
        }):
            raise SkillEditError(
                "skill_read_only",
                f"Skill '{name}' is {source_kind} and is read-only.",
            )
        raw_root = str(skill.get("base_dir") or "").strip()
        if not raw_root:
            raise SkillEditError("skill_path_unavailable", "The skill source is unavailable.")
        source_root = Path(raw_root).expanduser()
        if source_root.is_symlink() or os.path.islink(str(source_root)):
            raise SkillEditError("skill_path_rejected", "The skill source is not a safe directory.")
        root = source_root.resolve()
        if not root.is_dir():
            raise SkillEditError("skill_path_rejected", "The skill source is not a safe directory.")
        return skill, root

    def _relative_path(self, root: Path, raw_path: Any) -> tuple[Path, str]:
        rendered = str(raw_path or "").strip().replace("\\", "/")
        if (
            not rendered
            or rendered.startswith("/")
            or rendered.startswith("//")
            or re.match(r"^[A-Za-z]:", rendered)
        ):
            raise SkillEditError("path_rejected", "Skill file paths must be relative to the skill.")
        parts = rendered.split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise SkillEditError("path_rejected", "Skill file path traversal is not allowed.")
        if any(_sensitive_component(part) for part in parts):
            raise SkillEditError("sensitive_path", "Sensitive skill files cannot be inspected or edited.")

        candidate = root.joinpath(*parts)
        cursor = root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink() or os.path.islink(str(cursor)):
                raise SkillEditError("symlink_rejected", "Symlinked skill paths cannot be edited.")
        resolved = candidate.resolve(strict=False)
        if not _is_under(resolved, root):
            raise SkillEditError("path_rejected", "The skill file escapes its skill directory.")
        if candidate.exists() and not candidate.is_file():
            raise SkillEditError("path_rejected", "Skill edits only operate on files.")
        return candidate, "/".join(parts)

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise SkillEditError("file_too_large", "The skill file exceeds the safe size limit.")
            return path.read_bytes()
        except SkillEditError:
            raise
        except (OSError, ValueError):
            raise SkillEditError("file_read_failed", "The skill file could not be read.") from None

    @classmethod
    def _read_text(cls, path: Path) -> str:
        raw = cls._read_bytes(path)
        if b"\x00" in raw:
            raise SkillEditError("binary_file", "Binary skill files cannot be edited as text.")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SkillEditError("binary_file", "Skill edits require UTF-8 text files.") from None

    @staticmethod
    def _sha256(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _parse_frontmatter(content: str, skill_name: str) -> None:
        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
        if not match:
            raise SkillEditError(
                "invalid_frontmatter",
                "SKILL.md must start with a closed YAML frontmatter block.",
            )
        frontmatter = match.group(1)
        try:
            if _HAS_YAML:
                data = yaml.safe_load(frontmatter) or {}
            else:
                data: Dict[str, Any] = {}
                for line in frontmatter.splitlines():
                    if ":" not in line or line.lstrip().startswith("#"):
                        continue
                    key, value = line.split(":", 1)
                    data[key.strip()] = value.strip().strip("\"'")
        except Exception:
            raise SkillEditError("invalid_frontmatter", "SKILL.md frontmatter is not valid YAML.") from None
        if not isinstance(data, dict):
            raise SkillEditError("invalid_frontmatter", "SKILL.md frontmatter must be a mapping.")
        declared_name = str(data.get("name") or "").strip()
        if declared_name != skill_name:
            raise SkillEditError(
                "invalid_frontmatter",
                "SKILL.md frontmatter name must match the registered skill.",
            )
        if not str(data.get("description") or "").strip():
            raise SkillEditError(
                "invalid_frontmatter",
                "SKILL.md frontmatter requires a description.",
            )

    @staticmethod
    def _validate_python(path_name: str, content: str) -> None:
        try:
            compile(content, f"<skill:{path_name}>", "exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            raise SkillEditError("validation_failed", f"Python syntax is invalid in {path_name}.") from exc

    @staticmethod
    def _validate_json(path_name: str, content: str) -> None:
        try:
            json.loads(content)
        except (TypeError, ValueError):
            raise SkillEditError("validation_failed", f"JSON syntax is invalid in {path_name}.") from None

    @staticmethod
    def _copy_safe_tree(source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for current, dirnames, filenames in os.walk(source, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(source)
            target_dir = destination / relative
            target_dir.mkdir(parents=True, exist_ok=True)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in _SENSITIVE_DIRS
                and not name.startswith(".")
                and not os.path.islink(str(current_path / name))
            ]
            for name in filenames:
                source_file = current_path / name
                if _sensitive_component(name) or os.path.islink(str(source_file)):
                    continue
                target_file = target_dir / name
                try:
                    shutil.copy2(source_file, target_file)
                except OSError:
                    raise SkillEditError("validation_failed", "The skill could not be staged for validation.") from None

    @classmethod
    def _validate_api_importability(cls, staged_root: Path, skill_name: str) -> None:
        api_path = staged_root / "api.py"
        if not api_path.is_file():
            return
        module_name = f"limebot_skill_validation_{re.sub(r'[^a-zA-Z0-9_]', '_', skill_name)}_{uuid.uuid4().hex}"
        previous_path = list(sys.path)
        module = None
        try:
            sys.path.insert(0, str(staged_root))
            spec = importlib.util.spec_from_file_location(module_name, api_path)
            if spec is None or spec.loader is None:
                raise SkillEditError("validation_failed", "The skill API handler could not be loaded.")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except SkillEditError:
            raise
        except Exception:
            raise SkillEditError("validation_failed", "The skill API handler could not be imported.") from None
        finally:
            sys.path[:] = previous_path
            sys.modules.pop(module_name, None)

    def _validate_candidate(
        self,
        root: Path,
        skill_name: str,
        candidate_files: Dict[str, Optional[str]],
        touched: Iterable[str],
    ) -> None:
        skill_md = candidate_files.get("SKILL.md")
        if skill_md is None:
            skill_md_path = root / "SKILL.md"
            if not skill_md_path.is_file():
                raise SkillEditError("invalid_frontmatter", "Every skill must keep a SKILL.md file.")
            skill_md = self._read_text(skill_md_path)
        self._parse_frontmatter(skill_md, skill_name)

        for path_name in touched:
            content = candidate_files.get(path_name)
            if content is None:
                continue
            suffix = Path(path_name).suffix.lower()
            if suffix in {".py", ".pyi"}:
                self._validate_python(path_name, content)
            elif suffix == ".json":
                self._validate_json(path_name, content)

        with tempfile.TemporaryDirectory(prefix="limebot-skill-validate-", dir=str(root.parent)) as temporary:
            staged_root = Path(temporary) / root.name
            self._copy_safe_tree(root, staged_root)
            for path_name, content in candidate_files.items():
                target = staged_root / Path(path_name)
                if content is None:
                    target.unlink(missing_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="")
            self._validate_api_importability(staged_root, skill_name)

    @staticmethod
    def _safe_atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
        temporary: Optional[Path] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=str(path.parent),
                prefix=f".{path.name}.limebot-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, stat.S_IMODE(mode))
            except OSError:
                pass
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def inspect(
        self,
        skill_name: str,
        path: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        skill, root = self._skill_context(skill_name, require_editable=False)
        inventory: list[Dict[str, Any]] = []
        truncated = False
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in _SENSITIVE_DIRS
                and not name.startswith(".")
                and not os.path.islink(str(current_path / name))
            ]
            for filename in sorted(filenames):
                if _sensitive_component(filename) or os.path.islink(str(current_path / filename)):
                    continue
                if len(inventory) >= MAX_INVENTORY_FILES:
                    truncated = True
                    break
                target = current_path / filename
                try:
                    raw = self._read_bytes(target)
                    kind = "binary" if b"\x00" in raw else "text"
                    if kind == "text":
                        try:
                            raw.decode("utf-8")
                        except UnicodeDecodeError:
                            kind = "binary"
                    inventory.append(
                        {
                            "path": _posix_relative(target.relative_to(root)),
                            "kind": kind,
                            "bytes": len(raw),
                            "sha256": self._sha256(raw),
                        }
                    )
                except SkillEditError as exc:
                    inventory.append(
                        {
                            "path": _posix_relative(target.relative_to(root)),
                            "kind": "unavailable",
                            "error_code": exc.code,
                        }
                    )
            if truncated:
                break

        selected_path = path or "SKILL.md"
        target, relative = self._relative_path(root, selected_path)
        content = self._read_text(target)
        if start_line is not None or end_line is not None:
            try:
                start = max(1, int(start_line or 1))
                end = int(end_line) if end_line is not None else None
            except (TypeError, ValueError):
                raise SkillEditError("invalid_line_range", "start_line and end_line must be integers.") from None
            if end is not None and end < start:
                raise SkillEditError("invalid_line_range", "end_line must be greater than or equal to start_line.")
            lines = content.splitlines(keepends=True)
            content = "".join(lines[start - 1 : end])
        content = content[:MAX_INSPECT_CHARS]
        if len(content) == MAX_INSPECT_CHARS:
            content += "\n... [content truncated] ..."

        return {
            "status": "success",
            "code": "skill_inspected",
            "skill": str(skill.get("name") or skill_name),
            "source_kind": str(skill.get("source_kind") or "legacy-local"),
            "editable": bool(skill.get("editable", False)),
            "files": inventory,
            "files_truncated": truncated,
            "selected_file": relative,
            "content": _safe_text(content, root),
        }

    def edit(self, skill_name: str, changes: Any) -> Dict[str, Any]:
        skill, root = self._skill_context(skill_name)
        if not isinstance(changes, list) or not changes:
            raise SkillEditError("invalid_changes", "changes must contain at least one operation.")
        if len(changes) > MAX_CHANGE_OPERATIONS:
            raise SkillEditError("invalid_changes", "Too many skill edit operations.")

        candidate: Dict[str, Optional[str]] = {}
        originals: Dict[str, Optional[str]] = {}
        original_bytes: Dict[str, Optional[bytes]] = {}
        touched: list[str] = []
        replacement_groups: Dict[str, list[Dict[str, Any]]] = {}
        seen_non_replace: set[str] = set()

        for raw in changes:
            if not isinstance(raw, dict):
                raise SkillEditError("invalid_changes", "Each skill edit operation must be an object.")
            operation = str(raw.get("operation") or raw.get("op") or "").strip().lower()
            if operation not in {"replace", "create", "delete"}:
                raise SkillEditError("invalid_changes", "Skill edit operation must be replace, create, or delete.")
            target, relative = self._relative_path(root, raw.get("path"))
            if relative not in touched:
                touched.append(relative)
            exists = target.exists()
            if exists and not target.is_file():
                raise SkillEditError("path_rejected", "Skill edits only operate on files.")
            if target.is_symlink() or os.path.islink(str(target)):
                raise SkillEditError("symlink_rejected", "Symlinked skill paths cannot be edited.")

            if relative not in originals:
                if exists:
                    raw_bytes = self._read_bytes(target)
                    try:
                        originals[relative] = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        raise SkillEditError("binary_file", "Skill edits require UTF-8 text files.") from None
                    original_bytes[relative] = raw_bytes
                else:
                    originals[relative] = None
                    original_bytes[relative] = None

            expected = str(raw.get("expected_sha256") or "").strip().lower()
            if expected:
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise SkillEditError("stale_match", "expected_sha256 must be a SHA-256 from inspection.")
                actual = self._sha256(original_bytes[relative] or b"") if exists else ""
                if actual != expected:
                    raise SkillEditError("stale_match", "The skill file changed; inspect it again before editing.")

            if operation == "replace":
                if relative in seen_non_replace:
                    raise SkillEditError("invalid_changes", "A file cannot mix create/delete with replacements in one edit.")
                if not exists:
                    raise SkillEditError("stale_match", "The replacement target no longer exists; inspect the skill again.")
                replacement_groups.setdefault(relative, []).append(
                    {
                        "old_text": raw.get("old_text"),
                        "new_text": raw.get("new_text"),
                        "occurrence": raw.get("occurrence", 1),
                        "replace_all": raw.get("replace_all", False),
                    }
                )
            elif relative in seen_non_replace or relative in replacement_groups:
                raise SkillEditError("invalid_changes", "A file cannot mix create/delete with replacements in one edit.")
            else:
                seen_non_replace.add(relative)
                if operation == "create":
                    if exists:
                        raise SkillEditError("stale_match", "The create target already exists; inspect the skill again.")
                    content = raw.get("content", raw.get("new_text"))
                    if not isinstance(content, str):
                        raise SkillEditError("invalid_changes", "create requires string content.")
                    candidate[relative] = content
                else:
                    if not exists:
                        raise SkillEditError("stale_match", "The delete target does not exist.")
                    if relative.lower() == "skill.md":
                        raise SkillEditError("invalid_changes", "SKILL.md cannot be deleted from a skill.")
                    candidate[relative] = None

        for relative, operations in replacement_groups.items():
            original = originals[relative]
            if original is None:
                raise SkillEditError("stale_match", "The replacement target does not exist.")
            if any(not isinstance(item.get("old_text"), str) or not item.get("old_text") for item in operations):
                raise SkillEditError("stale_match", "replace requires exact non-empty old_text.")
            if any(not isinstance(item.get("new_text"), str) for item in operations):
                raise SkillEditError("invalid_changes", "replace requires string new_text.")
            try:
                updated, _ = apply_text_edits(original, operations)
            except EditValidationError as exc:
                raise SkillEditError("stale_match", "The exact replacement text did not match the current skill file.") from exc
            candidate[relative] = updated

        total_bytes = 0
        for relative, content in candidate.items():
            if content is not None:
                if "\x00" in content:
                    raise SkillEditError("binary_file", "Binary content cannot be written by edit_skill.")
                encoded = content.encode("utf-8")
                total_bytes += len(encoded)
                if len(encoded) > MAX_FILE_BYTES:
                    raise SkillEditError("file_too_large", "The edited skill file exceeds the safe size limit.")
                if _contains_known_secret(content):
                    raise SkillEditError("credential_value", "Known environment credential values cannot be written to a skill.")
        if total_bytes > MAX_TOTAL_EDIT_BYTES:
            raise SkillEditError("edit_too_large", "The combined skill edit exceeds the safe size limit.")

        changed: Dict[str, Optional[str]] = {}
        for relative, content in candidate.items():
            if originals.get(relative) != content:
                changed[relative] = content
        if not changed:
            return {
                "status": "success",
                "code": "already_applied",
                "skill": str(skill.get("name") or skill_name),
                "source_kind": str(skill.get("source_kind") or "legacy-local"),
                "editable": True,
                "files": touched,
                "verification": {"status": "not_needed"},
            }

        final_files: Dict[str, Optional[str]] = dict(candidate)
        self._validate_candidate(root, skill_name, final_files, touched)

        snapshots: Dict[Path, Optional[tuple[bytes, int]]] = {}
        targets: Dict[str, Path] = {}
        for relative in changed:
            target, _ = self._relative_path(root, relative)
            targets[relative] = target
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise SkillEditError("symlink_rejected", "A skill file changed shape while the edit was prepared.")
                raw = self._read_bytes(target)
                snapshots[target] = (raw, stat.S_IMODE(target.stat().st_mode))
            else:
                snapshots[target] = None

        created_dirs: set[Path] = set()
        for target in targets.values():
            parent = target.parent
            while parent != root and not parent.exists():
                created_dirs.add(parent)
                parent = parent.parent

        def restore_runtime() -> None:
            try:
                if hasattr(self.registry, "refresh"):
                    self.registry.refresh()
                else:
                    self.registry.discover_and_load()
            except Exception:
                pass
            if self.agent is not None and hasattr(self.agent, "_refresh_tool_definitions"):
                try:
                    self.agent._refresh_tool_definitions()
                except Exception:
                    pass

        try:
            for relative, content in changed.items():
                target = targets[relative]
                self._relative_path(root, relative)
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    mode = snapshots[target][1] if snapshots[target] else 0o600
                    self._safe_atomic_write(target, content, mode=mode)

            if hasattr(self.registry, "refresh"):
                self.registry.refresh()
            else:
                self.registry.discover_and_load()
            loaded = self.registry.get_skill(skill_name) if self.registry is not None else None
            if not isinstance(loaded, dict):
                raise SkillEditError("reload_failed", "The edited skill was not present after reload.")
            if self.agent is not None and hasattr(self.agent, "_refresh_tool_definitions"):
                self.agent._refresh_tool_definitions()
        except SkillEditError:
            self._rollback(snapshots, created_dirs, root)
            restore_runtime()
            raise
        except Exception:
            self._rollback(snapshots, created_dirs, root)
            restore_runtime()
            raise SkillEditError("reload_failed", "The edited skill could not be reloaded; all changes were rolled back.") from None

        diff_parts: list[str] = []
        for relative, content in changed.items():
            before = originals.get(relative) or ""
            after = content or ""
            diff_parts.append(
                unified_text_diff(
                    before,
                    after,
                    fromfile=f"{relative} (before)",
                    tofile=f"{relative} (after)",
                    max_chars=4_000,
                )
            )
        diff = "\n".join(part for part in diff_parts if part)
        return {
            "status": "success",
            "code": "skill_reloaded",
            "skill": str(skill.get("name") or skill_name),
            "source_kind": str(skill.get("source_kind") or "legacy-local"),
            "editable": True,
            "files": list(changed),
            "verification": {"status": "passed", "registry_reloaded": True},
            "diff": _safe_text(diff[:6_000], root),
        }

    @staticmethod
    def _rollback(
        snapshots: Dict[Path, Optional[tuple[bytes, int]]],
        created_dirs: Optional[Iterable[Path]] = None,
        root: Optional[Path] = None,
    ) -> None:
        for target, snapshot in reversed(list(snapshots.items())):
            try:
                if snapshot is None:
                    target.unlink(missing_ok=True)
                    continue
                raw, mode = snapshot
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary: Optional[Path] = None
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=str(target.parent),
                    prefix=f".{target.name}.limebot-rollback-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temporary, mode)
                except OSError:
                    pass
                os.replace(temporary, target)
            except OSError:
                # The original error is more useful than a secondary rollback
                # failure; the registry reload below will expose any issue.
                continue
        if root is None:
            return
        for directory in sorted(
            set(created_dirs or ()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                if (
                    directory != root
                    and _is_under(directory, root)
                    and directory.is_dir()
                    and not directory.is_symlink()
                ):
                    directory.rmdir()
            except OSError:
                # Only empty directories created by this transaction are
                # removed; any pre-existing or newly populated directory is
                # left intact.
                continue
