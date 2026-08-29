"""Copy-on-write workspaces for subagent execution.

The parent workspace is never used as the write target while an isolated
subagent context is active.  This module captures a bounded applyable
changeset instead of silently merging. The parent must call
``apply_workspace_changeset`` (or ``IsolatedWorkspace.apply_to_source``)
to write the capture into the live tree, then leftover clones are deleted.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.context import workspace_context
from core.file_edits import unified_text_diff

_PENDING_WORKSPACES: Dict[str, "IsolatedWorkspace"] = {}


_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".vercel",
        "temp",
        "logs",
        "data",
        "persona",
    }
)
_IGNORED_FILES = frozenset(
    {
        "limebot.json",
        "package-lock.json",
        "config.py",
        "secrets.py",
    }
)
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_DIFF_CHARS = 48_000
_MAX_DIFF_FILE_BYTES = 200_000


def _copy_ignore(directory: str, names: list[str]) -> list[str]:
    ignored = []
    for name in names:
        lowered = name.lower()
        candidate = Path(directory) / name
        if (
            lowered in _IGNORED_DIRS
            or lowered in _IGNORED_FILES
            or lowered.startswith(".env")
            or candidate.is_symlink()
        ):
            ignored.append(name)
    return ignored


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_ignored_snapshot_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = relative.parts
    if any(part.lower() in _IGNORED_DIRS for part in parts[:-1]):
        return True
    filename = parts[-1].lower() if parts else ""
    return filename in _IGNORED_FILES or filename.startswith(".env")


def _snapshot_sync(root: Path) -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}
    if not root.exists():
        return snapshot
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or _is_ignored_snapshot_path(path, root)
        ):
            continue
        try:
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES:
                snapshot[path.relative_to(root).as_posix()] = {
                    "sha256": "oversize",
                    "size": size,
                }
                continue
            content = path.read_bytes()
            snapshot[path.relative_to(root).as_posix()] = {
                "sha256": _sha256_bytes(content),
                "size": len(content),
                "content": content if len(content) <= _MAX_DIFF_FILE_BYTES else None,
            }
        except (OSError, ValueError):
            continue
    return snapshot


def _capture_sync(
    root: Path,
    baseline: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    current = _snapshot_sync(root)
    changed_paths = sorted(set(baseline) | set(current))
    changed_paths = [
        relative
        for relative in changed_paths
        if baseline.get(relative, {}).get("sha256")
        != current.get(relative, {}).get("sha256")
    ]

    changed_files = []
    diff_budget = _MAX_DIFF_CHARS
    for relative in changed_paths:
        before_meta = baseline.get(relative) or {}
        after_meta = current.get(relative) or {}
        # Snapshot content is retained only for small text-like files.  The
        # baseline cannot be reconstructed from the modified clone afterward.
        before_bytes = before_meta.get("content") if before_meta else b""
        after_bytes = after_meta.get("content") if after_meta else b""
        status = "modified"
        if not before_meta:
            status = "added"
            before_bytes = b""
        elif not after_meta:
            status = "deleted"
            after_bytes = b""

        entry: Dict[str, Any] = {
            "path": relative,
            "status": status,
            "before_sha256": before_meta.get("sha256", ""),
            "after_sha256": after_meta.get("sha256", ""),
            "before_size": before_meta.get("size", 0),
            "after_size": after_meta.get("size", 0),
        }
        if before_bytes is not None and after_bytes is not None:
            try:
                before_text = before_bytes.decode("utf-8")
                after_text = after_bytes.decode("utf-8")
                entry["before_text"] = before_text
                entry["after_text"] = after_text
                if diff_budget > 0:
                    diff = unified_text_diff(
                        before_text,
                        after_text,
                        fromfile=f"a/{relative}",
                        tofile=f"b/{relative}",
                        max_chars=diff_budget,
                    )
                    entry["diff"] = diff
                    diff_budget -= len(diff)
            except UnicodeDecodeError:
                entry["binary"] = True
        changed_files.append(entry)

    added = sum(1 for item in changed_files if item["status"] == "added")
    modified = sum(1 for item in changed_files if item["status"] == "modified")
    deleted = sum(1 for item in changed_files if item["status"] == "deleted")
    return {
        "status": "changed" if changed_files else "clean",
        "changed_files": changed_files,
        "summary": {
            "total": len(changed_files),
            "added": added,
            "modified": modified,
            "deleted": deleted,
        },
    }


def register_pending_workspace(workspace: "IsolatedWorkspace") -> str:
    workspace_id = workspace.workspace_id
    _PENDING_WORKSPACES[workspace_id] = workspace
    return workspace_id


def unregister_pending_workspace(workspace: "IsolatedWorkspace") -> None:
    _PENDING_WORKSPACES.pop(workspace.workspace_id, None)


def get_pending_workspace(workspace_id: str) -> Optional["IsolatedWorkspace"]:
    return _PENDING_WORKSPACES.get(str(workspace_id or "").strip())


def latest_pending_workspace() -> Optional["IsolatedWorkspace"]:
    if not _PENDING_WORKSPACES:
        return None
    return next(reversed(_PENDING_WORKSPACES.values()))


def list_pending_workspaces() -> List["IsolatedWorkspace"]:
    return list(_PENDING_WORKSPACES.values())


def leftover_clone_paths(source_root: Optional[Path] = None) -> List[Path]:
    """Return clone directories that are not a still-pending IsolatedWorkspace."""
    pending_roots = {workspace.root.resolve() for workspace in _PENDING_WORKSPACES.values()}
    found: List[Path] = []
    search_roots = []
    if source_root is not None:
        search_roots.append(Path(source_root) / "temp")
    search_roots.append(Path.cwd() / "temp")
    seen: set[Path] = set()
    for root in search_roots:
        if not root.is_dir():
            continue
        for path in root.glob("bakeoff-*-isolated"):
            resolved = path.resolve()
            if resolved in seen or resolved in pending_roots:
                continue
            seen.add(resolved)
            found.append(path)
    tmp = Path(tempfile.gettempdir())
    for path in tmp.glob("limebot-subagent-*"):
        resolved = path.resolve()
        if resolved in seen or resolved in pending_roots:
            continue
        seen.add(resolved)
        found.append(path)
    return found


def cleanup_leftover_clones(source_root: Optional[Path] = None) -> List[str]:
    removed: List[str] = []
    for path in leftover_clone_paths(source_root):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
            removed.append(str(path))
        except OSError:
            continue
    return removed


@dataclass
class IsolatedWorkspace:
    """A temporary source snapshot used by one subagent execution."""

    source_root: Path
    root: Path
    label: str
    baseline: Dict[str, Dict[str, Any]]
    workspace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    last_capture: Optional[Dict[str, Any]] = None
    _cleaned: bool = False

    @classmethod
    async def create(cls, source_root: Path, label: str = "subagent"):
        source_root = Path(source_root).resolve()
        if not source_root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {source_root}")

        def _prepare() -> tuple[Path, Dict[str, Dict[str, Any]]]:
            root = Path(tempfile.mkdtemp(prefix="limebot-subagent-"))
            try:
                shutil.copytree(
                    source_root,
                    root,
                    ignore=_copy_ignore,
                    symlinks=False,
                    dirs_exist_ok=True,
                )
                return root, _snapshot_sync(root)
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise

        import asyncio

        root, baseline = await asyncio.to_thread(_prepare)
        return cls(
            source_root=source_root,
            root=root,
            label=str(label or "subagent")[:120],
            baseline=baseline,
        )

    def context_value(self) -> Dict[str, str]:
        return {
            "mode": "copy",
            "root": str(self.root),
            "source_root": str(self.source_root),
            "label": self.label,
            "workspace_id": self.workspace_id,
        }

    def activate(self):
        return workspace_context.set(self.context_value())

    @staticmethod
    def deactivate(token) -> None:
        workspace_context.reset(token)

    async def capture(self) -> Dict[str, Any]:
        import asyncio

        capture = await asyncio.to_thread(_capture_sync, self.root, self.baseline)
        capture["workspace_id"] = self.workspace_id
        self.last_capture = capture
        return capture

    def retain(self) -> str:
        """Keep this clone until the parent applies or cleans it up."""
        return register_pending_workspace(self)

    def is_pending(self) -> bool:
        return get_pending_workspace(self.workspace_id) is self

    async def cleanup(self) -> None:
        unregister_pending_workspace(self)
        if self._cleaned:
            return
        self._cleaned = True
        import asyncio

        await asyncio.to_thread(shutil.rmtree, self.root, ignore_errors=True)

    def applyable_file_text(self, item: Dict[str, Any]) -> Optional[str]:
        relative = str(item.get("path") or "")
        if not relative or item.get("status") == "deleted":
            return None
        clone_path = self.root / relative
        if clone_path.is_file():
            try:
                return clone_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        after_text = item.get("after_text")
        return after_text if isinstance(after_text, str) else None

    def applyable_before_text(self, item: Dict[str, Any]) -> Optional[str]:
        before_text = item.get("before_text")
        return before_text if isinstance(before_text, str) else None

    def report_metadata(self, capture: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "mode": "copy",
            "label": self.label,
            "workspace_id": self.workspace_id,
            "status": capture.get("status", "clean"),
            "summary": capture.get("summary", {}),
            "apply_tool": "apply_workspace_changeset",
            "changed_files": [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "path",
                        "status",
                        "before_sha256",
                        "after_sha256",
                        "before_size",
                        "after_size",
                    }
                }
                for item in capture.get("changed_files", [])
            ],
        }
