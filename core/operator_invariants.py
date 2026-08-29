"""Loop-enforced operator invariants: verify, merge, and no silent fail.

These are not prompt advice. AgentLoop consults this module before a
mutating / coding / isolated-subagent task run can be marked completed
or reported Ready. Casual small talk does not apply.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MUTATION_TOOLS = frozenset(
    {
        "edit_file",
        "write_file",
        "delete_file",
        "edit_skill",
        "create_skill",
        "apply_workspace_changeset",
    }
)
# Spreadsheet creation is a delivered binary artifact, not a text edit that
# verify_files can check. It does not by itself require a verify gate.
SELF_VERIFIED_MUTATION_TOOLS = frozenset({"create_spreadsheet"})
VERIFY_TOOLS = frozenset({"verify_files", "diagnose_files"})
PROOF_TOOLS = frozenset({"run_command", "run_steps"})
PROOF_COMMAND_RE = re.compile(
    r"\b(test|pytest|unittest|lint|build|compile|check|verify|py_compile)\b",
    re.IGNORECASE,
)
PASSED_VERIFY_STATUSES = frozenset({"passed", "success", "completed", "ok"})
WORKSPACE_ID_RE = re.compile(r"workspace_id[=:]?\s*([A-Za-z0-9_-]{6,})", re.IGNORECASE)

MISSING_VERIFY_PREFIX = (
    "Verification was not run on the files this turn changed"
)
UNAPPLIED_CHANGESET_PREFIX = "Unapplied isolated changeset"


@dataclass(frozen=True)
class InvariantFinding:
    """One user-visible invariant failure. ``text`` is the exact chat line."""

    code: str
    text: str
    workspace_id: str = ""
    paths: Tuple[str, ...] = ()


@dataclass
class InvariantVerdict:
    applies: bool
    ok: bool
    findings: List[InvariantFinding] = field(default_factory=list)
    mutated_paths: List[str] = field(default_factory=list)
    verified_paths: List[str] = field(default_factory=list)
    pending_workspace_ids: List[str] = field(default_factory=list)

    @property
    def visible_texts(self) -> List[str]:
        return [item.text for item in self.findings if str(item.text or "").strip()]

    @property
    def missing_verify(self) -> bool:
        return any(item.code == "missing_verify" for item in self.findings)

    @property
    def missing_apply(self) -> bool:
        return any(item.code == "unapplied_changeset" for item in self.findings)


def _normalize_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    try:
        return Path(raw).as_posix().lstrip("./")
    except (OSError, ValueError):
        return raw.lstrip("./")


def _path_key(value: Any) -> str:
    normalized = _normalize_path(value)
    if not normalized:
        return ""
    return Path(normalized).name.lower()


def _parse_json_object(text: Any) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw.startswith("{") and not raw.startswith("["):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _paths_from_mapping(payload: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    single = payload.get("path")
    if single:
        found.append(_normalize_path(single))
    for key in ("paths", "files", "changed_files", "applied"):
        items = payload.get(key)
        if isinstance(items, str):
            found.append(_normalize_path(items))
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                found.append(_normalize_path(item))
            elif isinstance(item, dict):
                found.append(_normalize_path(item.get("path") or item.get("file")))
    return [item for item in found if item]


def _paths_from_write_text(text: str) -> List[str]:
    match = re.search(r"Successfully (?:wrote to|deleted) '([^']+)'", str(text or ""))
    if match:
        return [_normalize_path(match.group(1))]
    return []


def _tool_args_paths(args: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(args, dict):
        return []
    return _paths_from_mapping(args)


def _outcome_success(outcome: Any) -> bool:
    return bool(getattr(outcome, "success", False))


def _outcome_tool(outcome: Any) -> str:
    return str(getattr(outcome, "tool", "") or "")


def _outcome_text(outcome: Any) -> str:
    return str(
        getattr(outcome, "diagnostic_tail", "")
        or getattr(outcome, "diagnostic_head", "")
        or getattr(outcome, "verification_detail", "")
        or ""
    )


def extract_mutated_paths(
    outcomes: Sequence[Any],
    history: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[str]:
    """Return display paths this turn actually changed."""

    found: List[str] = []
    for outcome in outcomes or []:
        tool = _outcome_tool(outcome)
        if tool not in MUTATION_TOOLS or not _outcome_success(outcome):
            continue
        payload = _parse_json_object(_outcome_text(outcome))
        if payload:
            found.extend(_paths_from_mapping(payload))
        found.extend(_paths_from_write_text(_outcome_text(outcome)))

    for name, args, content, success in _iter_turn_tool_events(history or []):
        if name not in MUTATION_TOOLS or not success:
            continue
        found.extend(_tool_args_paths(args))
        payload = _parse_json_object(content)
        if payload:
            found.extend(_paths_from_mapping(payload))
        found.extend(_paths_from_write_text(content))

    return list(dict.fromkeys(item for item in found if item))


def extract_verified_paths(
    outcomes: Sequence[Any],
    history: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[str]:
    found: List[str] = []
    for outcome in outcomes or []:
        tool = _outcome_tool(outcome)
        if tool not in VERIFY_TOOLS or not _outcome_success(outcome):
            continue
        payload = _parse_json_object(
            getattr(outcome, "verification_detail", "") or _outcome_text(outcome)
        )
        if payload:
            found.extend(_paths_from_mapping(payload))
            files = payload.get("files")
            if isinstance(files, list):
                for item in files:
                    if isinstance(item, dict):
                        found.append(_normalize_path(item.get("path")))
    for name, args, content, success in _iter_turn_tool_events(history or []):
        if name not in VERIFY_TOOLS or not success:
            continue
        found.extend(_tool_args_paths(args))
        payload = _parse_json_object(content)
        if payload:
            found.extend(_paths_from_mapping(payload))
    return list(dict.fromkeys(item for item in found if item))


def _iter_turn_tool_events(
    history: Sequence[Dict[str, Any]],
) -> List[Tuple[str, Dict[str, Any], str, bool]]:
    """Walk this turn's tool calls from the last user message forward."""

    entries = list(history or [])
    start = 0
    for index, entry in enumerate(entries):
        if entry.get("role") == "user":
            start = index + 1
    pending_args: Dict[str, Dict[str, Any]] = {}
    events: List[Tuple[str, Dict[str, Any], str, bool]] = []
    for entry in entries[start:]:
        role = entry.get("role")
        if role == "assistant":
            for call in entry.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else {}
                name = str((function or {}).get("name") or "")
                raw_args = (function or {}).get("arguments") or "{}"
                parsed: Dict[str, Any] = {}
                if isinstance(raw_args, dict):
                    parsed = raw_args
                else:
                    try:
                        loaded = json.loads(raw_args)
                        if isinstance(loaded, dict):
                            parsed = loaded
                    except (TypeError, json.JSONDecodeError):
                        parsed = {}
                call_id = str(call.get("id") or "")
                if call_id:
                    pending_args[call_id] = parsed
        elif role == "tool":
            name = str(entry.get("name") or "")
            content = str(entry.get("content") or "")
            args = pending_args.get(str(entry.get("tool_call_id") or ""), {})
            success = not _looks_like_tool_error(name, content)
            events.append((name, args, content, success))
    return events


def _looks_like_tool_error(name: str, content: str) -> bool:
    text = str(content or "").strip()
    lowered = text.lower()
    if text.startswith(("Error:", "ACTION BLOCKED:", "ACTION CANCELLED:")):
        return True
    payload = _parse_json_object(text)
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").strip().lower()
        if status in {"error", "failed", "blocked", "cancelled"}:
            return True
        if name in VERIFY_TOOLS and status and status not in PASSED_VERIFY_STATUSES:
            return True
    if "exit code:" in lowered:
        match = re.search(r"exit code:\s*(-?\d+)", lowered)
        if match and match.group(1) not in {"0"}:
            return True
    return False


def turn_had_file_mutations(
    outcomes: Sequence[Any],
    history: Optional[Sequence[Dict[str, Any]]] = None,
) -> bool:
    if any(
        _outcome_success(item) and _outcome_tool(item) in MUTATION_TOOLS
        for item in outcomes or []
    ):
        return True
    return any(
        name in MUTATION_TOOLS and success
        for name, _args, _content, success in _iter_turn_tool_events(history or [])
    )


def has_verification_evidence(
    outcomes: Sequence[Any],
    *,
    mutated_paths: Optional[Sequence[str]] = None,
    history: Optional[Sequence[Dict[str, Any]]] = None,
) -> bool:
    """True when verify_files/diagnose_files or a proof command covered the turn."""

    mutated = [_normalize_path(item) for item in (mutated_paths or []) if item]
    verified = extract_verified_paths(outcomes, history)
    proof_command = False
    verify_tool_passed = False
    for outcome in outcomes or []:
        tool = _outcome_tool(outcome)
        if not _outcome_success(outcome):
            continue
        if tool in VERIFY_TOOLS:
            status = str(getattr(outcome, "verification_status", "") or "").lower()
            if not status or status in PASSED_VERIFY_STATUSES:
                verify_tool_passed = True
        if tool in PROOF_TOOLS:
            status = str(getattr(outcome, "verification_status", "") or "").lower()
            command_text = _outcome_text(outcome)
            if status in PASSED_VERIFY_STATUSES or PROOF_COMMAND_RE.search(command_text):
                proof_command = True
    if not verify_tool_passed:
        for name, args, content, success in _iter_turn_tool_events(history or []):
            if not success:
                continue
            if name in VERIFY_TOOLS:
                verify_tool_passed = True
            if name in PROOF_TOOLS:
                command = str((args or {}).get("command") or (args or {}).get("commands") or "")
                if PROOF_COMMAND_RE.search(command) or PROOF_COMMAND_RE.search(content):
                    proof_command = True
    if proof_command:
        return True
    if not verify_tool_passed:
        return False
    if not mutated:
        return True
    verified_keys = {_path_key(item) for item in verified if item}
    if not verified_keys:
        # A passing verify_files call without recoverable paths still counts
        # as proof that verification ran this turn.
        return True
    return any(_path_key(path) in verified_keys for path in mutated)


def extract_workspace_ids(outcomes: Sequence[Any], history: Optional[Sequence[Dict[str, Any]]] = None) -> List[str]:
    found: List[str] = []
    blobs: List[str] = []
    for outcome in outcomes or []:
        blobs.append(_outcome_text(outcome))
        if _outcome_tool(outcome) == "spawn_agent":
            blobs.append(str(getattr(outcome, "diagnostic_head", "") or ""))
    for name, args, content, _success in _iter_turn_tool_events(history or []):
        if name in {"spawn_agent", "apply_workspace_changeset"}:
            blobs.append(content)
            blobs.append(str((args or {}).get("workspace_id") or ""))
    for blob in blobs:
        for match in WORKSPACE_ID_RE.finditer(str(blob or "")):
            found.append(match.group(1))
        payload = _parse_json_object(blob)
        if payload:
            workspace_id = str(payload.get("workspace_id") or "").strip()
            if workspace_id:
                found.append(workspace_id)
    return list(dict.fromkeys(item for item in found if item))


def apply_succeeded_for(
    workspace_id: str,
    outcomes: Sequence[Any],
    history: Optional[Sequence[Dict[str, Any]]] = None,
) -> bool:
    wanted = str(workspace_id or "").strip()
    if not wanted:
        return False
    for outcome in outcomes or []:
        if _outcome_tool(outcome) != "apply_workspace_changeset":
            continue
        if not _outcome_success(outcome):
            continue
        text = _outcome_text(outcome)
        payload = _parse_json_object(text)
        if payload and str(payload.get("status") or "").lower() == "applied":
            applied_id = str(payload.get("workspace_id") or "").strip()
            if not applied_id or applied_id == wanted:
                return True
        if wanted in text:
            return True
    for name, args, content, success in _iter_turn_tool_events(history or []):
        if name != "apply_workspace_changeset" or not success:
            continue
        if str((args or {}).get("workspace_id") or "") == wanted:
            return True
        payload = _parse_json_object(content)
        if payload and str(payload.get("status") or "").lower() == "applied":
            applied_id = str(payload.get("workspace_id") or "").strip()
            if not applied_id or applied_id == wanted:
                return True
    return False


def pending_workspaces_for_turn(
    *,
    session_key: str = "",
    outcomes: Sequence[Any] = (),
    history: Optional[Sequence[Dict[str, Any]]] = None,
    pending: Optional[Iterable[Any]] = None,
) -> List[Any]:
    turn_ids = set(extract_workspace_ids(outcomes, history))
    session = str(session_key or "")
    matched: List[Any] = []
    for workspace in pending or []:
        workspace_id = str(getattr(workspace, "workspace_id", "") or "")
        label = str(getattr(workspace, "label", "") or "")
        capture = getattr(workspace, "last_capture", None) or {}
        changed = (
            str((capture or {}).get("status") or "") == "changed"
            or bool((capture or {}).get("changed_files"))
        )
        if not changed and hasattr(workspace, "last_capture"):
            # A retained workspace is already a changed clone.
            changed = True
        if not changed:
            continue
        if workspace_id in turn_ids or (session and label.startswith(session)):
            matched.append(workspace)
    return matched


def missing_verify_text(paths: Sequence[str]) -> str:
    listed = ", ".join(paths) if paths else "(changed files)"
    return (
        f"{MISSING_VERIFY_PREFIX}: {listed}. "
        "Call verify_files on those paths, or rerun the tests/commands this "
        "task used as proof, before claiming completion."
    )


def unapplied_changeset_text(workspace_id: str) -> str:
    return (
        f"{UNAPPLIED_CHANGESET_PREFIX} workspace_id={workspace_id}. "
        "The live tree is unchanged. Call apply_workspace_changeset("
        f"workspace_id={workspace_id}) or this task is blocked."
    )


def evaluate_operator_invariants(
    *,
    casual_turn: bool = False,
    plan_mode: bool = False,
    coding_turn: bool = False,
    coding_goal: bool = False,
    outcomes: Sequence[Any] = (),
    history: Optional[Sequence[Dict[str, Any]]] = None,
    session_key: str = "",
    pending_workspaces: Optional[Iterable[Any]] = None,
    unresolved_tool_failure: bool = False,
    iterations_limit_reached: bool = False,
) -> InvariantVerdict:
    """Decide whether a turn may claim success under the three invariants."""

    if plan_mode:
        return InvariantVerdict(applies=False, ok=True)
    mutated_paths = extract_mutated_paths(outcomes, history)
    had_mutations = bool(mutated_paths) or turn_had_file_mutations(outcomes, history)
    pending = pending_workspaces_for_turn(
        session_key=session_key,
        outcomes=outcomes,
        history=history,
        pending=pending_workspaces,
    )
    applies = bool(
        (not casual_turn)
        and (coding_turn or coding_goal or had_mutations or pending)
    )
    if casual_turn and not had_mutations and not pending:
        return InvariantVerdict(applies=False, ok=True)
    if not applies:
        return InvariantVerdict(applies=False, ok=True)

    findings: List[InvariantFinding] = []
    verified_paths = extract_verified_paths(outcomes, history)
    if had_mutations and not has_verification_evidence(
        outcomes, mutated_paths=mutated_paths, history=history
    ):
        findings.append(
            InvariantFinding(
                code="missing_verify",
                text=missing_verify_text(mutated_paths),
                paths=tuple(mutated_paths),
            )
        )

    pending_ids: List[str] = []
    for workspace in pending:
        workspace_id = str(getattr(workspace, "workspace_id", "") or "")
        if not workspace_id:
            continue
        pending_ids.append(workspace_id)
        if not apply_succeeded_for(workspace_id, outcomes, history):
            findings.append(
                InvariantFinding(
                    code="unapplied_changeset",
                    text=unapplied_changeset_text(workspace_id),
                    workspace_id=workspace_id,
                )
            )

    if iterations_limit_reached and had_mutations and not findings:
        findings.append(
            InvariantFinding(
                code="missing_verify",
                text=missing_verify_text(mutated_paths),
                paths=tuple(mutated_paths),
            )
        )

    # Tool failures are surfaced separately; they still prevent success.
    ok = not findings and not unresolved_tool_failure
    return InvariantVerdict(
        applies=True,
        ok=ok,
        findings=findings,
        mutated_paths=list(mutated_paths),
        verified_paths=list(verified_paths),
        pending_workspace_ids=pending_ids,
    )


def format_invariant_next_action(verdict: InvariantVerdict) -> str:
    if verdict.missing_apply:
        finding = next(item for item in verdict.findings if item.code == "unapplied_changeset")
        return (
            f"Call apply_workspace_changeset(workspace_id={finding.workspace_id}). "
            f"{finding.text}"
        )
    if verdict.missing_verify:
        finding = next(item for item in verdict.findings if item.code == "missing_verify")
        return finding.text
    if verdict.visible_texts:
        return verdict.visible_texts[0]
    return "Inspect the last diagnostic before claiming completion."


def ensure_visible_failures(
    reply: str,
    *extra_texts: Any,
    required_texts: Optional[Sequence[str]] = None,
) -> str:
    """Guarantee failure text is present in the user-visible reply."""

    visible = str(reply or "")
    required: List[str] = []
    for item in extra_texts:
        if item is None:
            continue
        if isinstance(item, InvariantVerdict):
            required.extend(item.visible_texts)
            continue
        if isinstance(item, InvariantFinding):
            required.append(item.text)
            continue
        if isinstance(item, (list, tuple)):
            required.extend(str(part) for part in item if str(part or "").strip())
            continue
        text = str(item or "").strip()
        if text:
            required.append(text)
    required.extend(str(item).strip() for item in (required_texts or []) if str(item or "").strip())
    missing = [text for text in required if text and text not in visible]
    if not missing:
        return visible
    appendix = "\n\n".join(missing)
    if visible.strip():
        return visible.rstrip() + "\n\n" + appendix
    return appendix


def collect_subagent_failures(history: Sequence[Dict[str, Any]]) -> List[str]:
    """Return child tool error/rejection text for the parent report."""

    failures: List[str] = []
    for name, _args, content, success in _iter_turn_tool_events(history):
        if success:
            continue
        text = str(content or "").strip()
        if not text:
            continue
        failures.append(f"{name}: {text}")
    return failures


def format_subagent_failure_block(failures: Sequence[str]) -> str:
    if not failures:
        return ""
    return "Child tool failure(s):\n" + "\n\n".join(failures)
