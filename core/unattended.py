"""Unattended execution policy for scheduled and explicitly unattended jobs.

Live chat stays confirmation-gated, including after a durable restart. Scheduled
or explicitly unattended jobs may execute sensitive tools only when the path or
command matches an explicit allowlist.
This is not a global autonomous switch.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.confirmation import SENSITIVE_TOOLS


def _split_csv(raw: str) -> List[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def is_unattended_turn(msg: Any) -> bool:
    metadata = getattr(msg, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return False
    return bool(
        metadata.get("is_scheduler")
        or metadata.get("unattended")
    )


def load_unattended_config(config: Any = None) -> Dict[str, List[str]]:
    path_allowlist: List[str] = []
    command_allowlist: List[str] = []
    if config is not None:
        unattended = getattr(config, "unattended", None)
        if unattended is not None:
            path_allowlist.extend(getattr(unattended, "path_allowlist", []) or [])
            command_allowlist.extend(getattr(unattended, "command_allowlist", []) or [])
        whitelist = getattr(config, "whitelist", None)
        if whitelist is not None and not path_allowlist:
            path_allowlist.extend(getattr(whitelist, "allowed_paths", []) or [])
    if not path_allowlist:
        path_allowlist.extend(_split_csv(os.getenv("UNATTENDED_PATH_ALLOWLIST", "")))
    if not command_allowlist:
        command_allowlist.extend(_split_csv(os.getenv("UNATTENDED_COMMAND_ALLOWLIST", "")))
    return {
        "path_allowlist": [str(item) for item in path_allowlist if str(item).strip()],
        "command_allowlist": [str(item) for item in command_allowlist if str(item).strip()],
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def path_is_allowlisted(target: str, allowlist: Iterable[str]) -> bool:
    if not target or not allowlist:
        return False
    try:
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
    except (OSError, ValueError):
        return False
    for raw in allowlist:
        try:
            root = Path(raw).expanduser()
            if not root.is_absolute():
                root = Path.cwd() / root
            if _is_relative_to(path, root):
                return True
        except (OSError, ValueError):
            continue
    return False


def command_is_allowlisted(command: str, allowlist: Iterable[str]) -> bool:
    raw = str(command or "").strip()
    if not raw or not allowlist:
        return False
    try:
        parts = shlex.split(raw, posix=os.name != "nt")
    except ValueError:
        parts = raw.split()
    binary = parts[0] if parts else raw
    candidates = {raw.lower(), binary.lower(), Path(binary).name.lower()}
    allowed = {item.lower() for item in allowlist}
    if candidates & allowed:
        return True
    for item in allowed:
        if raw.lower().startswith(item) or binary.lower().startswith(item):
            return True
    return False


def evaluate_unattended_tool(
    function_name: str,
    function_args: Optional[Dict[str, Any]] = None,
    config: Any = None,
) -> Dict[str, Any]:
    """Return an approval decision for an unattended sensitive tool call."""
    policy = load_unattended_config(config)
    args = function_args or {}
    allowed = False
    detail = "unattended_not_allowlisted"

    if function_name not in SENSITIVE_TOOLS:
        return {
            "allowed": True,
            "requires_confirmation": False,
            "reason": "unattended_readonly",
            "policy_profile": "unattended",
        }

    if function_name in {
        "write_file",
        "edit_file",
        "delete_file",
        "create_spreadsheet",
        "apply_workspace_changeset",
    }:
        target = str(args.get("path") or args.get("source_root") or "")
        if function_name == "apply_workspace_changeset" and not target:
            target = str(Path.cwd())
        allowed = path_is_allowlisted(target, policy["path_allowlist"])
        detail = "unattended_path_allowlist" if allowed else "unattended_path_denied"
    elif function_name == "run_command":
        allowed = command_is_allowlisted(
            str(args.get("command") or ""), policy["command_allowlist"]
        )
        detail = "unattended_command_allowlist" if allowed else "unattended_command_denied"
    elif function_name == "run_steps":
        commands = args.get("commands") or []
        if isinstance(commands, str):
            commands = [commands]
        allowed = bool(commands) and all(
            command_is_allowlisted(str(item or ""), policy["command_allowlist"])
            for item in commands
        )
        detail = "unattended_command_allowlist" if allowed else "unattended_command_denied"
    elif function_name == "cron_remove":
        allowed = "cron_remove" in {item.lower() for item in policy["command_allowlist"]}
        detail = "unattended_cron_allowlist" if allowed else "unattended_cron_denied"

    return {
        "allowed": allowed,
        "requires_confirmation": False,
        "reason": detail,
        "policy_profile": "unattended",
    }
