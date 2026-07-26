"""Copy-on-write workspaces for subagent execution.

The parent workspace is never used as the write target while an isolated
subagent context is active.  This module intentionally captures a bounded
diff instead of silently merging changes; applying a patch remains an
explicit, approval-aware parent action.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from core.context import workspace_context
from core.file_edits import unified_text_diff


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


@dataclass
class IsolatedWorkspace:
    """A temporary source snapshot used by one subagent execution."""

    source_root: Path
    root: Path
    label: str
    baseline: Dict[str, Dict[str, Any]]
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
        }

    def activate(self):
        return workspace_context.set(self.context_value())

    @staticmethod
    def deactivate(token) -> None:
        workspace_context.reset(token)

    async def capture(self) -> Dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(_capture_sync, self.root, self.baseline)

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        import asyncio

        await asyncio.to_thread(shutil.rmtree, self.root, ignore_errors=True)

    def report_metadata(self, capture: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "mode": "copy",
            "label": self.label,
            "status": capture.get("status", "clean"),
            "summary": capture.get("summary", {}),
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
