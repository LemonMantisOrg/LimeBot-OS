"""Authoritative state for one visible LimeBot turn.

The agent may perform several model/tool attempts while pursuing one durable
task.  A turn is the short-lived delivery unit for the originating channel.
Keeping its terminal transition in one small object prevents a late stream
chunk, a continuation, or a tool error from changing an already settled turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


# ``retrying`` means this visible turn ended with a progress update and a
# durable continuation was queued.  It is terminal for this turn, but not for
# the underlying TaskRun.
TURN_STATUSES = frozenset(
    {"running", "completed", "retrying", "failed", "blocked", "cancelled"}
)
TURN_TERMINAL_STATUSES = frozenset(
    {"completed", "retrying", "failed", "blocked", "cancelled"}
)


@dataclass
class TurnState:
    """Monotonic state and terminal fence for one assistant delivery."""

    turn_id: str
    message_id: str
    session_key: str
    phase: str = "receiving"
    status: str = "running"
    sequence: int = 0
    terminal_emitted: bool = False

    def set_phase(self, phase: str) -> None:
        value = str(phase or "").strip()
        if value:
            self.phase = value[:80]
            self.sequence += 1

    def finish(self, status: str, *, error_code: str = "") -> Optional[Dict[str, str | int | bool]]:
        """Return the one terminal event payload, or ``None`` if already closed."""

        if self.terminal_emitted:
            return None
        normalized = str(status or "failed").strip().lower()
        if normalized not in TURN_TERMINAL_STATUSES:
            normalized = "failed"
        self.status = normalized
        self.phase = "terminal"
        self.sequence += 1
        self.terminal_emitted = True
        payload: Dict[str, str | int | bool] = {
            "type": "turn_terminal",
            "terminal": True,
            "turn_status": normalized,
            "turn_phase": self.phase,
            "terminal_sequence": self.sequence,
        }
        if error_code:
            payload["error_code"] = str(error_code)[:160]
        return payload


def normalize_turn_status(value: str, default: str = "completed") -> str:
    """Normalize a task/turn status to the small public turn vocabulary."""

    normalized = str(value or "").strip().lower()
    if normalized in TURN_STATUSES:
        return normalized
    return default if default in TURN_STATUSES else "completed"
