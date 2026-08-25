"""Durable task-run state for work that must survive a single LLM turn.

The inbound job queue answers "was this message accepted?".  A task run
answers the more important question: "is the user's original goal complete?".
The two identities intentionally remain separate so a continuation can reuse
the same goal and checkpoint without replaying the original message as a new
side effect.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


QUEUED = "queued"
RUNNING = "running"
PLANNING = "planning"
EXECUTING = "executing"
VERIFYING = "verifying"
REPAIRING = "repairing"
RETRYING = "retrying"
COMPLETED = "completed"
BLOCKED = "blocked"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, BLOCKED, CANCELLED})
ACTIVE_STATES = frozenset(
    {QUEUED, RUNNING, PLANNING, EXECUTING, VERIFYING, REPAIRING, RETRYING}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    goal TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    plan TEXT NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    checkpoint TEXT NOT NULL,
    transcript_cursor INTEGER NOT NULL DEFAULT 0,
    workspace_id TEXT,
    provider TEXT,
    slice_count INTEGER NOT NULL DEFAULT 0,
    corrective_failures INTEGER NOT NULL DEFAULT 0,
    max_slices INTEGER NOT NULL DEFAULT 20,
    max_corrective_failures INTEGER NOT NULL DEFAULT 5,
    last_error TEXT,
    next_action TEXT,
    result_preview TEXT,
    metadata TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    heartbeat_at REAL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_task_runs_updated ON task_runs(updated_at);
"""


@dataclass
class TaskRun:
    run_id: str
    status: str
    phase: str
    goal: str
    acceptance_criteria: List[str] = field(default_factory=list)
    plan: List[str] = field(default_factory=list)
    current_step: int = 0
    checkpoint: Dict[str, Any] = field(default_factory=dict)
    transcript_cursor: int = 0
    workspace_id: str = ""
    provider: str = ""
    slice_count: int = 0
    corrective_failures: int = 0
    max_slices: int = 20
    max_corrective_failures: int = 5
    last_error: str = ""
    next_action: str = ""
    result_preview: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    heartbeat_at: float = 0.0
    completed_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def resumable(self) -> bool:
        return self.status in ACTIVE_STATES

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def is_coding_goal(goal: str) -> bool:
    return bool(
        re.search(
            r"\b(code|coding|program|repo|repository|bug|fix|implement|edit|patch|"
            r"refactor|test|pytest|lint|build|compile|function|skill|git|pull|merge)\b",
            str(goal or ""),
            re.IGNORECASE,
        )
    )


def derive_acceptance_criteria(goal: str) -> List[str]:
    """Produce conservative criteria without pretending the LLM said success."""
    text = str(goal or "").strip()
    criteria = ["The original user request is satisfied."]
    lowered = text.lower()
    if is_coding_goal(text):
        criteria.append("The changed files pass the available verification checks.")
    if re.search(r"\b(test|pytest|lint|typecheck|build|compile)\b", lowered):
        criteria.append("The explicitly requested test, lint, typecheck, or build passes.")
    if re.search(r"\b(send|deliver|attach|download|export|file|document|image)\b", lowered):
        criteria.append("The requested artifact is delivered to the originating channel.")
    if re.search(r"\b(create|write|edit|delete|update|cancel|approve|deny|pull|merge)\b", lowered):
        criteria.append("The requested state-changing operation is confirmed by its result.")
    return list(dict.fromkeys(criteria))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, parsed)


def _row_to_run(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        phase=str(row["phase"]),
        goal=str(row["goal"] or ""),
        acceptance_criteria=list(_loads(row["acceptance_criteria"], [])),
        plan=list(_loads(row["plan"], [])),
        current_step=int(row["current_step"] or 0),
        checkpoint=dict(_loads(row["checkpoint"], {}) or {}),
        transcript_cursor=int(row["transcript_cursor"] or 0),
        workspace_id=str(row["workspace_id"] or ""),
        provider=str(row["provider"] or ""),
        slice_count=int(row["slice_count"] or 0),
        corrective_failures=int(row["corrective_failures"] or 0),
        max_slices=int(row["max_slices"] or 20),
        max_corrective_failures=int(row["max_corrective_failures"] or 5),
        last_error=str(row["last_error"] or ""),
        next_action=str(row["next_action"] or ""),
        result_preview=str(row["result_preview"] or ""),
        metadata=dict(_loads(row["metadata"], {}) or {}),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        heartbeat_at=float(row["heartbeat_at"] or 0),
        completed_at=float(row["completed_at"] or 0),
    )


class TaskRunStore:
    """Small SQLite store with atomic checkpoint updates."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    def get(self, run_id: str) -> Optional[TaskRun]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM task_runs WHERE run_id = ?", (str(run_id),)
                ).fetchone()
                return _row_to_run(row) if row else None
            finally:
                conn.close()

    def create_or_get(
        self,
        run_id: Optional[str],
        goal: str,
        *,
        session_key: str = "",
        workspace_id: str = "",
        provider: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        max_slices: int = 20,
        max_corrective_failures: int = 5,
    ) -> TaskRun:
        stable_id = str(run_id or uuid.uuid4().hex).strip() or uuid.uuid4().hex
        existing = self.get(stable_id)
        if existing is not None:
            return existing
        now = time.time()
        meta = dict(metadata or {})
        meta.setdefault("session_key", str(session_key or ""))
        record = TaskRun(
            run_id=stable_id,
            status=QUEUED,
            phase=PLANNING,
            goal=str(goal or "").strip(),
            acceptance_criteria=derive_acceptance_criteria(goal),
            workspace_id=str(workspace_id or ""),
            provider=str(provider or ""),
            max_slices=_positive_int(max_slices, 20),
            max_corrective_failures=_positive_int(max_corrective_failures, 5),
            metadata=meta,
            created_at=now,
            updated_at=now,
            heartbeat_at=now,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO task_runs (
                        run_id, status, phase, goal, acceptance_criteria, plan,
                        current_step, checkpoint, transcript_cursor, workspace_id,
                        provider, slice_count, corrective_failures, max_slices,
                        max_corrective_failures, last_error, next_action,
                        result_preview, metadata, created_at, updated_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.status,
                        record.phase,
                        record.goal,
                        _json(record.acceptance_criteria),
                        _json(record.plan),
                        record.current_step,
                        _json(record.checkpoint),
                        record.transcript_cursor,
                        record.workspace_id,
                        record.provider,
                        record.slice_count,
                        record.corrective_failures,
                        record.max_slices,
                        record.max_corrective_failures,
                        record.last_error,
                        record.next_action,
                        record.result_preview,
                        _json(record.metadata),
                        record.created_at,
                        record.updated_at,
                        record.heartbeat_at,
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM task_runs WHERE run_id = ?", (stable_id,)
                ).fetchone()
                return _row_to_run(row) if row else record
            finally:
                conn.close()
        return record

    def checkpoint(
        self,
        run_id: str,
        *,
        phase: Optional[str] = None,
        status: Optional[str] = None,
        current_step: Optional[int] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        transcript_cursor: Optional[int] = None,
        plan: Optional[Iterable[str]] = None,
        next_action: Optional[str] = None,
        last_error: Optional[str] = None,
        result_preview: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
        increment_slice: bool = False,
        increment_corrective_failure: bool = False,
    ) -> Optional[TaskRun]:
        current = self.get(run_id)
        if current is None:
            return None
        now = time.time()
        merged_meta = dict(current.metadata)
        if metadata_update:
            merged_meta.update(metadata_update)
        next_status = status or current.status
        next_phase = phase or current.phase
        next = TaskRun(
            **{
                **current.to_dict(),
                "status": next_status,
                "phase": next_phase,
                "current_step": current_step if current_step is not None else current.current_step,
                "checkpoint": checkpoint if checkpoint is not None else current.checkpoint,
                "transcript_cursor": transcript_cursor if transcript_cursor is not None else current.transcript_cursor,
                "plan": list(plan) if plan is not None else current.plan,
                "next_action": next_action if next_action is not None else current.next_action,
                "last_error": last_error if last_error is not None else current.last_error,
                "result_preview": result_preview if result_preview is not None else current.result_preview,
                "metadata": merged_meta,
                "slice_count": current.slice_count + (1 if increment_slice else 0),
                "corrective_failures": current.corrective_failures + (1 if increment_corrective_failure else 0),
                "updated_at": now,
                "heartbeat_at": now,
            }
        )
        if next.status in TERMINAL_STATES:
            next.completed_at = now
        self._write(next)
        return next

    def _write(self, record: TaskRun) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE task_runs SET status=?, phase=?, acceptance_criteria=?, plan=?,
                        current_step=?, checkpoint=?, transcript_cursor=?, workspace_id=?,
                        provider=?, slice_count=?, corrective_failures=?, max_slices=?,
                        max_corrective_failures=?, last_error=?, next_action=?,
                        result_preview=?, metadata=?, updated_at=?, heartbeat_at=?, completed_at=?
                    WHERE run_id=?
                    """,
                    (
                        record.status,
                        record.phase,
                        _json(record.acceptance_criteria),
                        _json(record.plan),
                        record.current_step,
                        _json(record.checkpoint),
                        record.transcript_cursor,
                        record.workspace_id,
                        record.provider,
                        record.slice_count,
                        record.corrective_failures,
                        record.max_slices,
                        record.max_corrective_failures,
                        record.last_error,
                        record.next_action,
                        record.result_preview,
                        _json(record.metadata),
                        record.updated_at,
                        record.heartbeat_at,
                        record.completed_at or None,
                        record.run_id,
                    ),
                )
            finally:
                conn.close()

    def start(self, run_id: str, *, phase: str = EXECUTING) -> Optional[TaskRun]:
        return self.checkpoint(
            run_id,
            phase=phase,
            status=RUNNING,
            increment_slice=True,
        )

    def request_resume(
        self,
        run_id: str,
        *,
        phase: str,
        next_action: str,
        error: str = "",
        corrective_failure: bool = False,
        checkpoint: Optional[Dict[str, Any]] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskRun]:
        return self.checkpoint(
            run_id,
            phase=phase,
            status=RETRYING,
            next_action=next_action,
            last_error=error,
            checkpoint=checkpoint,
            metadata_update=metadata_update,
            increment_corrective_failure=corrective_failure,
        )

    def complete(self, run_id: str, *, result: str = "", checkpoint: Optional[Dict[str, Any]] = None) -> Optional[TaskRun]:
        return self.checkpoint(
            run_id,
            phase=VERIFYING,
            status=COMPLETED,
            next_action="",
            result_preview=str(result or "")[:1000],
            checkpoint=checkpoint,
        )

    def block(self, run_id: str, *, error: str, next_action: str = "") -> Optional[TaskRun]:
        return self.checkpoint(
            run_id,
            phase="blocked",
            status=BLOCKED,
            last_error=str(error or "")[:1000],
            next_action=next_action,
        )

    def cancel(self, run_id: str, *, reason: str = "Task cancelled.") -> Optional[TaskRun]:
        return self.checkpoint(
            run_id,
            phase="cancelled",
            status=CANCELLED,
            last_error=reason,
            next_action="",
        )

    def list_runs(self, *, statuses: Optional[Iterable[str]] = None, limit: int = 100) -> List[TaskRun]:
        with self._lock:
            conn = self._connect()
            try:
                if statuses:
                    values = list(statuses)
                    placeholders = ",".join("?" for _ in values)
                    rows = conn.execute(
                        f"SELECT * FROM task_runs WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                        (*values, int(limit)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM task_runs ORDER BY updated_at DESC LIMIT ?",
                        (int(limit),),
                    ).fetchall()
                return [_row_to_run(row) for row in rows]
            finally:
                conn.close()

    def recover_interrupted(self, *, stale_after: float = 90.0) -> List[TaskRun]:
        """Move stale in-flight runs back to retrying without losing checkpoints."""
        cutoff = time.time() - max(1.0, float(stale_after))
        recovered = []
        for run in self.list_runs(statuses=ACTIVE_STATES, limit=1000):
            if run.heartbeat_at and run.heartbeat_at > cutoff:
                continue
            updated = self.request_resume(
                run.run_id,
                phase=run.phase,
                next_action=run.next_action or "Resume from the last verified checkpoint.",
                error="Runtime restarted; resuming from the last durable checkpoint.",
                metadata_update={"recovered_from_restart": True},
            )
            if updated:
                recovered.append(updated)
        return recovered


_instance: Optional[TaskRunStore] = None
_instance_lock = threading.Lock()


def get_task_run_store(db_path: Optional[Path | str] = None) -> TaskRunStore:
    global _instance
    with _instance_lock:
        if _instance is None:
            if db_path is None:
                from core.runtime_paths import get_data_dir

                db_path = get_data_dir() / "task_runs.sqlite"
            _instance = TaskRunStore(db_path)
        return _instance


def reset_task_run_store() -> None:
    global _instance
    with _instance_lock:
        _instance = None
