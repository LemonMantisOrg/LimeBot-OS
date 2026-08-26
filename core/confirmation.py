"""
core/confirmation.py
────────────────────
Confirmation / approval-flow logic, extracted from AgentLoop.

Provides rich previews for file writes, file deletes, and shell
commands, plus the embed-field builder used by Discord and the web
dashboard when a sensitive tool requires user approval.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.redaction import redact_sensitive_text
from core.file_edits import EditValidationError, apply_text_edits, unified_text_diff


# Tools that always require explicit user approval unless whitelisted.
SENSITIVE_TOOLS = frozenset(
    {
        "delete_file",
        "edit_file",
        "run_command",
        "write_file",
        "create_spreadsheet",
        "cron_remove",
        "edit_skill",
    }
)

APPROVE_WORDS = frozenset(
    {"proceed", "yes", "approve", "confirm", "ok", "sure", "y", "go", "run", "do it"}
)

DENY_WORDS = frozenset({"no", "cancel", "deny", "stop", "reject", "n", "abort", "nope"})


class ConfirmationManager:
    """Builds confirmation previews and embed fields for sensitive tool calls.

    Holds a reference to the parent AgentLoop's *toolbox* (for path helpers)
    and *truncate_preview* / *safe_json_load* utilities.
    """

    def __init__(self, toolbox, truncate_fn, safe_json_load_fn) -> None:
        self.toolbox = toolbox
        self._truncate_preview = truncate_fn
        self._safe_json_load = safe_json_load_fn

    # ── Individual previews ───────────────────────────────────────────────

    def build_write_preview(self, function_args: dict) -> Dict[str, Any]:
        """Preview dict for a ``write_file`` confirmation."""
        target = str(function_args.get("path", "") or "")
        content = str(function_args.get("content", "") or "")
        preview: Dict[str, Any] = {
            "kind": "write_file",
            "path": target,
            "summary": f"Write {len(content)} chars to {target or '(missing path)'}",
            "content_preview": self._truncate_preview(content, 1200),
        }
        if not target:
            return preview

        try:
            path = Path(target).resolve()
        except Exception:
            return preview

        preview["path"] = self.toolbox._to_display_path(path)
        if not self.toolbox._is_path_allowed(path):
            preview["summary"] = f"Attempt to write outside allowed paths: {target}"
            preview["risk_flags"] = ["outside_allowed_paths"]
            return preview

        if not path.exists():
            preview["summary"] = f"Create new file {preview['path']}"
            preview["mode"] = "create"
            return preview

        preview["mode"] = "overwrite"
        try:
            before = path.read_text(encoding="utf-8")
            diff_lines = list(
                difflib.unified_diff(
                    before.splitlines(),
                    content.splitlines(),
                    fromfile=f"{preview['path']} (before)",
                    tofile=f"{preview['path']} (after)",
                    lineterm="",
                    n=2,
                )
            )
            changed_lines = sum(
                1
                for line in diff_lines
                if (line.startswith("+") or line.startswith("-"))
                and not line.startswith("+++")
                and not line.startswith("---")
            )
            preview["summary"] = (
                f"Overwrite {preview['path']} with ~{changed_lines} changed line(s)"
            )
            if diff_lines:
                preview["diff"] = self._truncate_preview("\n".join(diff_lines), 1800)
        except UnicodeDecodeError:
            preview["summary"] = f"Overwrite binary or non-UTF8 file {preview['path']}"
            preview["risk_flags"] = ["binary_or_non_utf8_target"]
        except Exception as e:
            preview["diff_error"] = str(e)

        return preview

    def build_edit_preview(self, function_args: dict) -> Dict[str, Any]:
        """Preview an exact patch without applying it."""
        target = str(function_args.get("path", "") or "")
        edits = function_args.get("edits")
        expected = str(function_args.get("expected_sha256", "") or "").lower()
        preview: Dict[str, Any] = {
            "kind": "edit_file",
            "path": target,
            "summary": f"Apply exact patch to {target or '(missing path)'}",
            "edit_count": len(edits) if isinstance(edits, list) else 0,
        }
        if not target:
            return preview

        try:
            path = Path(target).resolve()
        except Exception:
            return preview

        preview["path"] = self.toolbox._to_display_path(path)
        if not self.toolbox._is_path_allowed(path):
            preview["summary"] = f"Attempt to edit outside allowed paths: {target}"
            preview["risk_flags"] = ["outside_allowed_paths"]
            return preview
        if not path.exists() or not path.is_file():
            preview["summary"] = f"Edit target is missing or not a file: {preview['path']}"
            preview["risk_flags"] = ["path_missing"]
            return preview

        try:
            before_bytes = path.read_bytes()
            current_hash = hashlib.sha256(before_bytes).hexdigest()
            preview["sha256"] = current_hash
            if expected != current_hash:
                preview["summary"] = (
                    f"Reject stale edit for {preview['path']} "
                    f"(expected {expected or '(missing)'}, current {current_hash})"
                )
                preview["risk_flags"] = ["stale_edit"]
                return preview

            before = before_bytes.decode("utf-8")
            after, replacements = apply_text_edits(before, edits)
            preview["mode"] = "edit"
            preview["replacements"] = replacements
            preview["summary"] = (
                f"Apply {replacements} exact replacement(s) to {preview['path']}"
            )
            preview["diff"] = unified_text_diff(
                before,
                after,
                fromfile=f"{preview['path']} (before)",
                tofile=f"{preview['path']} (after)",
                max_chars=1_800,
            )
        except (UnicodeDecodeError, EditValidationError) as exc:
            preview["summary"] = f"Reject invalid edit for {preview['path']}: {exc}"
            preview["risk_flags"] = ["invalid_edit"]
        except Exception as exc:
            preview["summary"] = f"Could not preview edit for {preview['path']}: {exc}"
            preview["risk_flags"] = ["preview_error"]
        return preview

    def build_skill_edit_preview(self, function_args: dict) -> Dict[str, Any]:
        """Build a redacted, bounded preview for ``edit_skill`` approval."""

        skill_name = str(function_args.get("skill_name", "") or "").strip()[:120]
        changes = function_args.get("changes")
        rows: List[Dict[str, Any]] = []
        if isinstance(changes, list):
            for item in changes[:32]:
                if not isinstance(item, dict):
                    continue
                operation = str(item.get("operation") or item.get("op") or "").strip().lower()[:32]
                path = str(item.get("path") or "").strip().replace("\\", "/")[:240]
                if (
                    not path
                    or path.startswith("/")
                    or path.startswith("//")
                    or re.match(r"^[A-Za-z]:", path)
                    or ".." in path.split("/")
                ):
                    path = "<rejected path>"
                row: Dict[str, Any] = {"operation": operation, "path": path}
                if operation == "replace":
                    old_text = item.get("old_text")
                    new_text = item.get("new_text")
                    if isinstance(old_text, str):
                        row["old_chars"] = len(old_text)
                    if isinstance(new_text, str):
                        row["new_chars"] = len(new_text)
                elif operation == "create":
                    content = item.get("content", item.get("new_text"))
                    if isinstance(content, str):
                        row["content_chars"] = len(content)
                rows.append(row)

        file_count = len(rows)
        summary = f"Edit local skill {skill_name or '(missing name)'} in {file_count} file(s)"
        return {
            "kind": "edit_skill",
            "skill": skill_name,
            "summary": summary,
            "mode": "transactional",
            "affected_paths": [str(row.get("path") or "") for row in rows if row.get("path")],
            "changes": rows,
        }

    def build_spreadsheet_preview(self, function_args: dict) -> Dict[str, Any]:
        """Preview a native XLSX creation without persisting workbook contents."""
        target = str(function_args.get("path", "") or "")
        sheets = function_args.get("sheets")
        sheet_count = len(sheets) if isinstance(sheets, list) else 0
        preview: Dict[str, Any] = {
            "kind": "create_spreadsheet",
            "path": target,
            "summary": f"Create Excel workbook with {sheet_count} worksheet(s) at {target or '(missing path)'}",
        }
        if not target:
            return preview
        try:
            path = Path(target).resolve()
        except Exception:
            return preview
        preview["path"] = self.toolbox._to_display_path(path)
        if not self.toolbox._is_path_allowed(path):
            preview["summary"] = f"Attempt to write outside allowed paths: {target}"
            preview["risk_flags"] = ["outside_allowed_paths"]
            return preview
        preview["mode"] = "overwrite" if path.exists() else "create"
        return preview

    def build_delete_preview(self, function_args: dict) -> Dict[str, Any]:
        """Preview dict for a ``delete_file`` confirmation."""
        target = str(function_args.get("path", "") or "")
        preview: Dict[str, Any] = {
            "kind": "delete_file",
            "path": target,
            "summary": f"Delete {target or '(missing path)'}",
        }
        if not target:
            return preview

        try:
            path = Path(target).resolve()
        except Exception:
            return preview

        preview["path"] = self.toolbox._to_display_path(path)
        if not path.exists():
            preview["summary"] = f"Delete missing path {preview['path']}"
            preview["risk_flags"] = ["path_missing"]
            return preview

        if path.is_dir():
            try:
                item_count = sum(1 for _ in path.rglob("*"))
            except Exception:
                item_count = None
            preview["summary"] = f"Delete directory {preview['path']}" + (
                f" ({item_count} item(s))" if item_count is not None else ""
            )
            preview["target_type"] = "directory"
        else:
            preview["summary"] = (
                f"Delete file {preview['path']} ({path.stat().st_size} bytes)"
            )
            preview["target_type"] = "file"

        return preview

    @staticmethod
    def extract_command_paths(command: str, limit: int = 5) -> List[str]:
        """Extract filesystem paths mentioned in a shell *command* string."""
        try:
            tokens = shlex.split(command, posix=False)
        except Exception:
            tokens = command.split()

        paths: List[str] = []
        for token in tokens:
            cleaned = token.strip("\"'")
            if not cleaned or cleaned.startswith("-"):
                continue
            if re.match(r"^[A-Za-z]:[\\/]", cleaned) or cleaned.startswith(
                (".", "/", "\\")
            ):
                paths.append(cleaned)
            elif "/" in cleaned or "\\" in cleaned:
                paths.append(cleaned)
            if len(paths) >= limit:
                break
        return paths

    def build_command_preview(self, function_args: dict) -> Dict[str, Any]:
        """Preview dict for a ``run_command`` confirmation."""
        command = str(function_args.get("command", "") or "")
        display_command = redact_sensitive_text(command)
        cwd = str(function_args.get("cwd", ".") or ".")
        lowered = command.lower()
        risk_flags: List[str] = []

        risk_checks = [
            (
                "install_or_dependency_change",
                (
                    " install ",
                    "pip install",
                    "npm install",
                    "pnpm install",
                    "uv add",
                    "poetry add",
                ),
            ),
            (
                "filesystem_mutation",
                (
                    "del ",
                    "erase ",
                    " move ",
                    " ren ",
                    " copy ",
                    "git clean",
                    "rmdir",
                    "mkdir",
                    "touch ",
                ),
            ),
            (
                "git_state_change",
                (
                    "git commit",
                    "git push",
                    "git pull",
                    "git merge",
                    "git rebase",
                    "git cherry-pick",
                ),
            ),
            (
                "network_access",
                (
                    "curl ",
                    "wget ",
                    "invoke-webrequest",
                    "irm ",
                    "iwr ",
                    "http://",
                    "https://",
                ),
            ),
            (
                "long_running_process",
                (
                    "npm run dev",
                    "vite",
                    "uvicorn",
                    "python main.py",
                    "tail -f",
                    "watch ",
                ),
            ),
        ]
        padded = f" {lowered} "
        for flag, needles in risk_checks:
            if any(needle in padded or needle in lowered for needle in needles):
                risk_flags.append(flag)

        affected_paths = self.extract_command_paths(command)
        summary = f"Run command in {cwd}: {display_command or '(empty command)'}"
        if risk_flags:
            summary += f" [{', '.join(risk_flags)}]"

        return {
            "kind": "run_command",
            "command": display_command,
            "cwd": cwd,
            "summary": summary,
            "risk_flags": risk_flags,
            "affected_paths": affected_paths,
        }

    # ── Composite preview / embed ─────────────────────────────────────────

    def build_preview(
        self, function_name: str, function_args: dict, session_key: str
    ) -> Dict[str, Any]:
        """Return a rich preview dict for any sensitive *function_name*."""
        if function_name == "write_file":
            preview = self.build_write_preview(function_args)
        elif function_name == "edit_file":
            preview = self.build_edit_preview(function_args)
        elif function_name == "edit_skill":
            preview = self.build_skill_edit_preview(function_args)
        elif function_name == "create_spreadsheet":
            preview = self.build_spreadsheet_preview(function_args)
        elif function_name == "delete_file":
            preview = self.build_delete_preview(function_args)
        elif function_name == "run_command":
            preview = self.build_command_preview(function_args)
        else:
            preview = {
                "kind": function_name,
                "summary": f"Execute {function_name}",
                "args_preview": self._truncate_preview(
                    json.dumps(function_args, indent=2), 1200
                ),
            }
        preview["session_key"] = session_key
        return preview

    def build_embed(
        self,
        function_name: str,
        function_args: dict,
        session_key: str,
        preview: Optional[Dict[str, Any]] = None,
    ) -> list:
        """Build the embed fields list for a tool confirmation prompt."""
        preview = preview or self.build_preview(
            function_name, function_args, session_key
        )

        fields = [
            {
                "name": "Action",
                "value": preview.get("summary", function_name),
                "inline": False,
            }
        ]

        if function_name == "run_command":
            fields.append(
                {
                    "name": "Command",
                    "value": (
                        "```bash\n"
                        f"{self._truncate_preview(redact_sensitive_text(function_args.get('command', '')), 800)}\n"
                        "```"
                    ),
                    "inline": False,
                }
            )
            if preview.get("risk_flags"):
                fields.append(
                    {
                        "name": "Risk Flags",
                        "value": ", ".join(preview["risk_flags"]),
                        "inline": False,
                    }
                )
        elif function_name in {"write_file", "edit_file", "create_spreadsheet"}:
            if preview.get("path"):
                fields.append(
                    {
                        "name": "Target File",
                        "value": f"`{preview['path']}`",
                        "inline": False,
                    }
                )
            if preview.get("diff"):
                fields.append(
                    {
                        "name": "Diff Preview",
                        "value": f"```diff\n{self._truncate_preview(preview['diff'], 800)}\n```",
                        "inline": False,
                    }
                )
            elif preview.get("content_preview"):
                fields.append(
                    {
                        "name": "Content Preview",
                        "value": f"```\n{self._truncate_preview(preview['content_preview'], 800)}\n```",
                        "inline": False,
                    }
                )
        elif function_name == "edit_skill":
            if preview.get("skill"):
                fields.append(
                    {
                        "name": "Skill",
                        "value": f"`{preview['skill']}`",
                        "inline": False,
                    }
                )
            if preview.get("affected_paths"):
                fields.append(
                    {
                        "name": "Files",
                        "value": self._truncate_preview(", ".join(preview["affected_paths"]), 800),
                        "inline": False,
                    }
                )
        elif function_name == "delete_file" and preview.get("path"):
            fields.append(
                {
                    "name": "Target Path",
                    "value": f"`{preview['path']}`",
                    "inline": False,
                }
            )
        else:
            fields.append(
                {
                    "name": "Arguments",
                    "value": f"```json\n{self._truncate_preview(json.dumps(function_args, indent=2), 800)}\n```",
                    "inline": False,
                }
            )

        fields.append({"name": "Agent", "value": f"`{session_key}`", "inline": True})
        return fields
