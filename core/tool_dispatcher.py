"""
core/tool_dispatcher.py
───────────────────────
Tool routing, alias normalization, and browser/tag execution helpers,
extracted from AgentLoop.

The ToolDispatcher keeps the constants and routing logic that previously
lived at module level and in AgentLoop methods, making loop.py significantly
leaner.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple


# ── Module-level constants (re-exported so loop.py can import them) ───────

TOOL_RESULT_LIMITS: Dict[str, int] = {
    "capability_search": 4_000,
    "read_file": 8_000,
    "edit_file": 8_000,
    "inspect_skill": 4_000,
    "edit_skill": 8_000,
    "search_files": 5_000,
    "verify_files": 6_000,
    "diagnose_files": 8_000,
    "memory_search": 3_000,
    "browser_extract": 5_000,
    "browser_get_page_text": 5_000,
    "browser_snapshot": 3_000,
    "google_search": 2_000,
    "web_search": 6_000,
    "image_search": 2_500,
    "deep_research": 8_000,
    "run_command": 2_000,
    "browser_list_media": 1_000,
    "list_dir": 500,
    "generate_image": 2_000,
    "analyze_video": 30_000,
    "spawn_agent": 12_000,
}
DEFAULT_TOOL_RESULT_LIMIT = 2_000


def truncate_tool_result(text: Any, limit: int) -> str:
    """Keep both the setup and terminal diagnostic of oversized tool output."""
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value

    # Command runners place their exit code and the most useful assertion at
    # the end.  A leading-only truncation made repair decisions needlessly
    # blind, so reserve enough room for a meaningful head and tail.
    marker = "\n... [truncated diagnostic] ...\n"
    available = max(2, limit - len(marker))
    head_size = max(1, (available * 3) // 5)
    tail_size = max(1, available - head_size)
    return value[:head_size] + marker + value[-tail_size:]

# Browser tools operate on mutable, session-local state — caching is unsafe.
BROWSER_CACHEABLE: frozenset = frozenset()

TAG_COMPAT_TOOLS = frozenset({
    "save_memory",
    "log_memory",
    "save_soul",
    "save_identity",
    "save_mood",
    "save_relationship",
    "save_user",
})

TOOL_INTENT_RE = re.compile(
    r"\b("
    # Exact tool names
    r"capability_search|read_file|edit_file|write_file|delete_file|list_dir|search_files|verify_files|diagnose_files|run_command|memory_search|memory_save|"
    r"cron_add|cron_list|cron_remove|spawn_agent|generate_image|send_media|send_voice|analyze_video|"
    # Search tool names
    r"web_search|image_search|deep_research|"
    # Browser tool names
    r"browser_navigate|browser_click|browser_type|browser_snapshot|browser_scroll|"
    r"browser_wait|browser_press_key|browser_go_back|browser_tabs|browser_switch_tab|"
    r"browser_extract|browser_get_page_text|browser_list_media|google_search|"
    # System command keywords
    r"ls|pwd|cat|grep|find|mkdir|rm|cp|mv|npm|pip|python|bash|powershell|terminal|"
    r"command|directory|folder|path|cron|schedule|skill|"
    # Action verbs that imply tool usage
    r"search|browse|download|upload|install|uninstall|deploy|execute|"
    r"open|save|fetch|scrape|navigate|lookup|look\s+up|"
    # Object nouns that imply tool-backed actions
    r"image|photo|picture|screenshot|video|watch|transcript|website|webpage|internet|"
    r"url|http|www|\.com|\.org|\.net|"
    r"file|code|script|project|repo|"
    r"reminder|alarm|timer|notify"
    r")\b",
    re.IGNORECASE,
)

# Canonical aliases for tool names the LLM sometimes uses
TOOL_NAME_ALIASES: Dict[str, str] = {
    "ls": "list_dir",
    "dir": "list_dir",
    "list_files": "list_dir",
    "cat": "read_file",
    "open_file": "read_file",
    "show_file": "read_file",
    "edit": "edit_file",
    "apply_patch": "edit_file",
    "patch_file": "edit_file",
    "verify": "verify_files",
    "check_files": "verify_files",
    "validate_files": "verify_files",
    "diagnose": "diagnose_files",
    "diagnostics": "diagnose_files",
    "lint_files": "diagnose_files",
    "grep": "search_files",
    "rg": "search_files",
    "ripgrep": "search_files",
    "find_files": "search_files",
    "shell": "run_command",
    "terminal": "run_command",
    "exec": "run_command",
    "bash": "run_command",
    "powershell": "run_command",
    "cmd": "run_command",
    "websearch": "web_search",
    "search_web": "web_search",
    "imagesearch": "image_search",
    "search_images": "image_search",
    "research": "deep_research",
}

FILESYSTEM_ALIAS_ACTIONS: Dict[str, str] = {
    "list": "list_dir",
    "read": "read_file",
    "edit": "edit_file",
    "patch": "edit_file",
    "modify": "edit_file",
    "verify": "verify_files",
    "check": "verify_files",
    "diagnose": "diagnose_files",
    "diagnostics": "diagnose_files",
    "lint": "diagnose_files",
    "write": "write_file",
    "delete": "delete_file",
    "find": "search_files",
    "search": "search_files",
}


def normalize_tool_alias(
    function_name: str,
    function_args: dict,
    record_anomaly_fn,
    session_key: str,
) -> Tuple[str, dict]:
    """Normalize common alias tools back to canonical runtime tool names.

    Parameters
    ----------
    function_name:
        Raw tool name received from the LLM.
    function_args:
        Raw argument dict.
    record_anomaly_fn:
        Callable matching ``MetricsCollector.record_anomaly`` signature,
        used to log alias normalization events.
    session_key:
        Current session ID, forwarded to the anomaly recorder.

    Returns
    -------
    (canonical_name, normalized_args)
    """
    normalized_name = TOOL_NAME_ALIASES.get(function_name, function_name)
    if normalized_name == function_name and function_name.endswith("json"):
        trimmed_name = function_name[: -len("json")]
        normalized_name = TOOL_NAME_ALIASES.get(trimmed_name, trimmed_name)
    normalized_args = dict(function_args or {})

    if normalized_name != function_name:
        record_anomaly_fn(
            session_key,
            "tool_alias_normalized",
            detail=f"{function_name}->{normalized_name}",
        )

        if normalized_name == "list_dir":
            normalized_args = {
                "path": normalized_args.get("path")
                or normalized_args.get("directory")
                or normalized_args.get("cwd")
                or ".",
                **{
                    k: v
                    for k, v in normalized_args.items()
                    if k
                    in {
                        "limit",
                        "offset",
                        "include_hidden",
                        "sort_by",
                        "descending",
                        "folders_first",
                    }
                },
            }
        elif normalized_name == "read_file":
            normalized_args = {
                "path": normalized_args.get("path")
                or normalized_args.get("file")
                or normalized_args.get("filename")
                or "",
                "start_line": normalized_args.get("start_line")
                or normalized_args.get("line_start"),
                "end_line": normalized_args.get("end_line")
                or normalized_args.get("line_end"),
                **{
                    k: v
                    for k, v in normalized_args.items()
                    if k in {"max_chars", "include_hash"}
                },
            }
        elif normalized_name == "edit_file":
            normalized_args = {
                "path": normalized_args.get("path")
                or normalized_args.get("file")
                or normalized_args.get("filename")
                or "",
                "edits": normalized_args.get("edits")
                or normalized_args.get("patches")
                or [],
                "expected_sha256": normalized_args.get("expected_sha256")
                or normalized_args.get("sha256")
                or normalized_args.get("expected_hash")
                or "",
            }
        elif normalized_name == "verify_files":
            paths = normalized_args.get("paths")
            if paths is None:
                path = normalized_args.get("path") or normalized_args.get("file")
                paths = [path] if path else []
            normalized_args = {
                "paths": paths,
                **{
                    k: v
                    for k, v in normalized_args.items()
                    if k in {"include_diagnostics", "provider"}
                },
            }
        elif normalized_name == "diagnose_files":
            paths = normalized_args.get("paths")
            if paths is None:
                path = normalized_args.get("path") or normalized_args.get("file")
                paths = [path] if path else []
            normalized_args = {
                "paths": paths,
                **{
                    k: v
                    for k, v in normalized_args.items()
                    if k in {"provider", "timeout"}
                },
            }
        elif normalized_name == "search_files":
            normalized_args = {
                "query": normalized_args.get("query")
                or normalized_args.get("pattern")
                or normalized_args.get("text")
                or normalized_args.get("name")
                or "",
                "path": normalized_args.get("path", "."),
                "mode": normalized_args.get("mode", "content"),
                **{
                    k: v
                    for k, v in normalized_args.items()
                    if k in {"file_glob", "case_sensitive", "max_results"}
                },
            }
        elif normalized_name == "run_command":
            normalized_args = {
                "command": normalized_args.get("command")
                or normalized_args.get("cmd")
                or normalized_args.get("script")
                or "",
            }

    if normalized_name != "filesystem":
        return normalized_name, normalized_args

    # ── filesystem alias dispatch ─────────────────────────────────────
    action = str(normalized_args.pop("action", "") or "").strip().lower()
    if not action:
        record_anomaly_fn(
            session_key, "filesystem_alias_missing_action", detail=str(function_args)
        )
        return function_name, function_args

    mapped_name = FILESYSTEM_ALIAS_ACTIONS.get(action)
    if not mapped_name:
        record_anomaly_fn(
            session_key, "filesystem_alias_unsupported", detail=f"action={action}"
        )
        return function_name, function_args

    record_anomaly_fn(
        session_key, "filesystem_alias_normalized", detail=f"{action}->{mapped_name}"
    )

    if mapped_name == "list_dir":
        normalized_args = {
            "path": normalized_args.get("path", "."),
            **{
                k: v
                for k, v in normalized_args.items()
                if k
                in {
                    "limit",
                    "offset",
                    "include_hidden",
                    "sort_by",
                    "descending",
                    "folders_first",
                }
            },
        }
    elif mapped_name == "read_file":
        normalized_args = {
            "path": normalized_args.get("path", ""),
            **{
                k: v
                for k, v in normalized_args.items()
                if k in {"max_chars", "start_line", "end_line"}
            },
        }
    elif mapped_name == "write_file":
        normalized_args = {
            "path": normalized_args.get("path", ""),
            "content": normalized_args.get("content", ""),
        }
    elif mapped_name == "delete_file":
        normalized_args = {"path": normalized_args.get("path", "")}
    elif mapped_name == "search_files":
        normalized_args = {
            "query": normalized_args.get("query")
            or normalized_args.get("pattern")
            or "",
            "path": normalized_args.get("path", "."),
            "mode": normalized_args.get("mode", "content"),
            **{
                k: v
                for k, v in normalized_args.items()
                if k in {"file_glob", "case_sensitive", "max_results"}
            },
        }

    return mapped_name, normalized_args
