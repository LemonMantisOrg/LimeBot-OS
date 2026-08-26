"""Typed, progress-aware recovery policy for tool-driven task runs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set


DIAGNOSTIC_TOOLS = frozenset(
    {
        "capability_search",
        "read_file",
        "list_dir",
        "search_files",
        "verify_files",
        "diagnose_files",
        "inspect_skill",
    }
)
RECOVERY_TOOLS = frozenset(
    {
        "capability_search",
        "read_file",
        "list_dir",
        "search_files",
        "verify_files",
        "diagnose_files",
        "inspect_skill",
        "edit_file",
        "edit_skill",
        "write_file",
        "run_command",
    }
)

FAILURE_CATEGORIES = frozenset(
    {
        "invalid_arguments",
        "local_skill_defect",
        "dependency",
        "authentication",
        "permission_policy",
        "timeout",
        "transient_provider",
        "command_failure",
        "cancellation",
        "unknown",
    }
)


def classify_failure(tool: str, text: str) -> str:
    value = str(text or "").lower()
    if "cancel" in value or "timed out" in value or "[timeout]" in value:
        return "cancellation" if "cancel" in value else "timeout"
    if any(marker in value for marker in ("401", "403", "authentication", "unauthorized", "invalid api key", "credentials")):
        return "authentication"
    if any(marker in value for marker in ("permission", "policy denied", "not allowed", "access denied")):
        return "permission_policy"
    if any(marker in value for marker in ("429", "rate limit", "502", "503", "504", "overloaded", "connection reset", "temporarily unavailable")):
        return "transient_provider"
    if any(marker in value for marker in ("no module named", "module not found", "dependency", "not installed", "command not found")):
        return "dependency"
    if any(marker in value for marker in ("attributeerror", "typeerror", "unexpected keyword", "missing required", "invalid argument", "schema")):
        return "invalid_arguments"
    if tool in {"run_command", "verify_files", "diagnose_files"}:
        return "command_failure"
    if tool in {"inspect_skill", "edit_skill"} or "skill" in value:
        return "local_skill_defect"
    return "unknown"


@dataclass
class RecoveryState:
    original_goal: str
    failed_tool: str = ""
    failure_category: str = "unknown"
    diagnostic_evidence: List[str] = field(default_factory=list)
    action_signatures: List[str] = field(default_factory=list)
    mutations: List[str] = field(default_factory=list)
    inspected_tools: List[str] = field(default_factory=list)
    verification_state: str = "not_started"
    corrective_failures: int = 0
    max_corrective_failures: int = 5
    evidence_generation: int = 0
    phase: str = "observe"
    active: bool = False
    resolved: bool = False
    last_action_generation: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Optional[Dict[str, Any]], goal: str = "") -> "RecoveryState":
        if not isinstance(value, dict):
            return cls(original_goal=goal)
        fields = {key: value[key] for key in cls.__dataclass_fields__ if key in value}
        fields.setdefault("original_goal", goal)
        fields["diagnostic_evidence"] = list(fields.get("diagnostic_evidence") or [])[-12:]
        fields["action_signatures"] = list(fields.get("action_signatures") or [])[-32:]
        fields["mutations"] = list(fields.get("mutations") or [])[-16:]
        fields["inspected_tools"] = list(fields.get("inspected_tools") or [])[-16:]
        return cls(**fields)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def signature(tool: str, args: Dict[str, Any]) -> str:
        payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(f"{tool}:{payload}".encode("utf-8", "replace")).hexdigest()[:20]

    def observe(self, outcome: Any, args: Optional[Dict[str, Any]] = None) -> None:
        tool = str(getattr(outcome, "tool", "") or "")
        success = bool(getattr(outcome, "success", False))
        was_active = self.active
        signature = self.signature(tool, args or {})
        self.action_signatures.append(signature)
        self.action_signatures = self.action_signatures[-32:]
        self.last_action_generation[signature] = self.evidence_generation
        if success:
            if tool in DIAGNOSTIC_TOOLS:
                if tool not in self.inspected_tools:
                    self.inspected_tools.append(tool)
                    self.inspected_tools = self.inspected_tools[-16:]
                self.evidence_generation += 1
                self.phase = "verify" if tool in {"verify_files", "diagnose_files"} else "inspect"
                if tool in {"verify_files", "diagnose_files"}:
                    self.verification_state = "passed"
            elif tool in {"edit_file", "edit_skill", "write_file", "delete_file", "run_command"}:
                self.evidence_generation += 1
                self.mutations.append(tool)
                self.mutations = self.mutations[-16:]
                self.phase = "verify"
            return
        self.active = True
        self.resolved = False
        self.failed_tool = tool
        category = str(getattr(outcome, "failure_category", "") or "")
        self.failure_category = category if category in FAILURE_CATEGORIES else classify_failure(
            tool, getattr(outcome, "diagnostic_tail", "")
        )
        evidence = str(
            getattr(outcome, "diagnostic_tail", "")
            or getattr(outcome, "diagnostic_head", "")
            or ""
        ).strip()
        if evidence:
            self.diagnostic_evidence.append(evidence[:500])
            self.diagnostic_evidence = self.diagnostic_evidence[-12:]
        # The first failed operation is evidence that recovery is needed; it is
        # not itself a corrective failure. Only a failed action after recovery
        # has started consumes the bounded corrective budget. This distinction
        # prevents a single bad tool call from spending the entire repair
        # allowance before inspection can begin.
        if was_active and tool not in DIAGNOSTIC_TOOLS:
            self.corrective_failures += 1
        self.phase = "inspect" if self.failure_category in {"invalid_arguments", "local_skill_defect", "dependency"} else "repair"

    def gate(self, tool: str, args: Dict[str, Any]) -> Optional[str]:
        """Reject exact no-progress retries before they reach the tool."""
        if not self.active or self.resolved:
            return None
        signature = self.signature(tool, args)
        if signature in self.action_signatures and self.last_action_generation.get(signature) == self.evidence_generation:
            return "recovery_no_progress: identical action already failed without new evidence"
        if self.failure_category == "invalid_arguments" and tool == self.failed_tool:
            inspected = bool(
                set(self.inspected_tools)
                & {"read_file", "inspect_skill", "capability_search"}
            )
            if not inspected:
                return "source_inspection_required: inspect the tool schema or source before retrying"
        return None

    def allowed_tool_names(self, available_names: Iterable[str]) -> Set[str]:
        available = {str(name) for name in available_names}
        wanted = set(RECOVERY_TOOLS) | ({self.failed_tool} if self.failed_tool else set())
        return available & wanted

    def can_continue(self) -> bool:
        return self.corrective_failures < self.max_corrective_failures

    def resolve(self) -> None:
        self.active = False
        self.resolved = True
        self.phase = "resolved"
        self.verification_state = "passed"
