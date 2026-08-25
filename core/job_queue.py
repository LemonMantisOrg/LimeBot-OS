"""Durable SQLite job queue for crash-safe scheduled and queued work.

Work is persisted *before* the agent runs. Interrupted jobs are re-queued on
boot instead of fail-closed. Cron completion is recorded only after the agent
turn (or delegated sub-agent work) reaches a terminal state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.events import InboundMessage


QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})
ACTIVE_STATES = frozenset({QUEUED, RUNNING})

IRREVERSIBLE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "delete_file",
        "run_command",
        "create_spreadsheet",
        "cron_remove",
    }
)

TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "overloaded",
    "circuit is open",
    "provider circuit",
    "service unavailable",
    "econnreset",
    "econnrefused",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    cron_job_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT,
    lease_until REAL,
    heartbeat_at REAL,
    side_effects INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    next_retry_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_cron ON jobs(cron_job_id);
"""


def is_transient_error(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in TRANSIENT_ERROR_MARKERS)


def serialize_inbound(msg: InboundMessage) -> Dict[str, Any]:
    return {
        "channel": msg.channel,
        "sender_id": msg.sender_id,
        "chat_id": msg.chat_id,
        "content": msg.content,
        "media": list(msg.media or []),
        "metadata": dict(msg.metadata or {}),
    }


def deserialize_inbound(payload: Dict[str, Any]) -> InboundMessage:
    return InboundMessage(
        channel=str(payload.get("channel") or "web"),
        sender_id=str(payload.get("sender_id") or "system"),
        chat_id=str(payload.get("chat_id") or "system"),
        content=str(payload.get("content") or ""),
        media=list(payload.get("media") or []),
        metadata=dict(payload.get("metadata") or {}),
    )


@dataclass
class Job:
    id: str
    status: str
    kind: str
    payload: Dict[str, Any]
    cron_job_id: Optional[str] = None
    attempt: int = 0
    max_attempts: int = 3
    lease_owner: Optional[str] = None
    lease_until: Optional[float] = None
    heartbeat_at: Optional[float] = None
    side_effects: bool = False
    last_error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    next_retry_at: Optional[float] = None

    def to_message(self) -> InboundMessage:
        msg = deserialize_inbound(self.payload)
        metadata = dict(msg.metadata or {})
        metadata["durable_job_id"] = self.id
        metadata["durable"] = True
        if self.cron_job_id:
            metadata["is_scheduler"] = True
            metadata["original_job_id"] = self.cron_job_id
        # Live chat jobs stay confirmation-gated after resume. Only scheduled
        # or already-unattended work keeps the unattended flag.
        metadata["unattended"] = bool(
            metadata.get("unattended")
            or metadata.get("is_scheduler")
            or self.kind == "cron"
        )
        msg.metadata = metadata
        return msg

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "kind": self.kind,
            "payload": self.payload,
            "cron_job_id": self.cron_job_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "lease_owner": self.lease_owner,
            "lease_until": self.lease_until,
            "heartbeat_at": self.heartbeat_at,
            "side_effects": self.side_effects,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "next_retry_at": self.next_retry_at,
        }


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        status=row["status"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        cron_job_id=row["cron_job_id"],
        attempt=int(row["attempt"] or 0),
        max_attempts=int(row["max_attempts"] or 3),
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
        heartbeat_at=row["heartbeat_at"],
        side_effects=bool(row["side_effects"]),
        last_error=row["last_error"],
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        next_retry_at=row["next_retry_at"],
    )


class DurableJobQueue:
    """Process-local SQLite queue with exclusive leases and honest retries."""

    def __init__(self, db_path: Path | str, *, default_lease_seconds: float = 45.0):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_lease_seconds = float(default_lease_seconds)
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

    def enqueue(
        self,
        msg: InboundMessage,
        *,
        kind: str = "inbound",
        cron_job_id: Optional[str] = None,
        max_attempts: int = 3,
        job_id: Optional[str] = None,
        unattended: Optional[bool] = None,
    ) -> Job:
        """Persist work before the in-memory bus sees it."""
        now = time.time()
        metadata = dict(msg.metadata or {})
        durable_id = job_id or str(metadata.get("durable_job_id") or uuid.uuid4().hex)
        metadata["durable_job_id"] = durable_id
        metadata["durable"] = True
        if cron_job_id:
            metadata["is_scheduler"] = True
            metadata["original_job_id"] = cron_job_id
        if unattended is None:
            unattended = bool(
                metadata.get("unattended")
                or metadata.get("is_scheduler")
                or kind == "cron"
            )
        metadata["unattended"] = bool(unattended)
        msg.metadata = metadata
        payload = serialize_inbound(msg)
        job = Job(
            id=durable_id,
            status=QUEUED,
            kind=kind,
            payload=payload,
            cron_job_id=cron_job_id,
            max_attempts=max(1, int(max_attempts)),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, status, kind, payload, cron_job_id, attempt,
                        max_attempts, side_effects, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?, ?)
                    """,
                    (
                        job.id,
                        job.status,
                        job.kind,
                        json.dumps(job.payload, default=str),
                        job.cron_job_id,
                        job.max_attempts,
                        job.created_at,
                        job.updated_at,
                    ),
                )
            finally:
                conn.close()
        return job

    def recover_interrupted(self, *, now: Optional[float] = None) -> List[Job]:
        """Re-queue running jobs left behind by a crash or expired lease."""
        now = time.time() if now is None else float(now)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = ?
                       OR (status = ? AND lease_until IS NOT NULL AND lease_until < ?)
                    """,
                    (RUNNING, RUNNING, now),
                ).fetchall()
                recovered: List[Job] = []
                for row in rows:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, lease_owner = NULL, lease_until = NULL,
                            heartbeat_at = NULL, updated_at = ?, next_retry_at = NULL
                        WHERE id = ?
                        """,
                        (QUEUED, now, row["id"]),
                    )
                    job = _row_to_job(row)
                    job.status = QUEUED
                    job.lease_owner = None
                    job.lease_until = None
                    job.heartbeat_at = None
                    job.updated_at = now
                    job.next_retry_at = None
                    recovered.append(job)
                return recovered
            finally:
                conn.close()

    def list_ready(self, *, now: Optional[float] = None) -> List[Job]:
        now = time.time() if now is None else float(now)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = ?
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY created_at ASC
                    """,
                    (QUEUED, now),
                ).fetchall()
                return [_row_to_job(row) for row in rows]
            finally:
                conn.close()

    def claim(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Optional[Job]:
        now = time.time() if now is None else float(now)
        lease_until = now + float(lease_seconds or self.default_lease_seconds)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    return None
                if row["status"] != QUEUED:
                    return None
                if row["next_retry_at"] is not None and float(row["next_retry_at"]) > now:
                    return None
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, lease_owner = ?, lease_until = ?,
                        heartbeat_at = ?, attempt = attempt + 1,
                        started_at = COALESCE(started_at, ?), updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        RUNNING,
                        worker_id,
                        lease_until,
                        now,
                        now,
                        now,
                        job_id,
                        QUEUED,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                return _row_to_job(updated) if updated else None
            finally:
                conn.close()

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> bool:
        now = time.time() if now is None else float(now)
        lease_until = now + float(lease_seconds or self.default_lease_seconds)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET heartbeat_at = ?, lease_until = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND lease_owner = ?
                    """,
                    (now, lease_until, now, job_id, RUNNING, worker_id),
                )
                return cursor.rowcount == 1
            finally:
                conn.close()

    def mark_side_effect(self, job_id: str, tool_name: str) -> None:
        if tool_name not in IRREVERSIBLE_TOOLS:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE jobs SET side_effects = 1, updated_at = ? WHERE id = ?",
                    (time.time(), job_id),
                )
            finally:
                conn.close()

    def finish(self, job_id: str, status: str, error: Optional[str] = None) -> Optional[Job]:
        if status not in TERMINAL_STATES:
            raise ValueError(f"finish() requires a terminal status, got {status}")
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, last_error = ?, finished_at = ?, updated_at = ?,
                        lease_owner = NULL, lease_until = NULL
                    WHERE id = ? AND status NOT IN (?, ?, ?)
                    """,
                    (
                        status,
                        error,
                        now,
                        now,
                        job_id,
                        SUCCEEDED,
                        FAILED,
                        CANCELLED,
                    ),
                )
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return _row_to_job(row) if row else None
            finally:
                conn.close()

    def update_payload_metadata(
        self, job_id: str, metadata_update: Dict[str, Any]
    ) -> Optional[Job]:
        """Atomically add internal continuation metadata to a queued job."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (str(job_id),)
                ).fetchone()
                if row is None or row["status"] in TERMINAL_STATES:
                    return _row_to_job(row) if row else None
                payload = json.loads(row["payload"])
                metadata = dict(payload.get("metadata") or {})
                metadata.update(dict(metadata_update or {}))
                payload["metadata"] = metadata
                now = time.time()
                conn.execute(
                    "UPDATE jobs SET payload = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(payload, default=str), now, str(job_id)),
                )
                updated = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (str(job_id),)
                ).fetchone()
                return _row_to_job(updated) if updated else None
            finally:
                conn.close()

    def requeue_continuation(
        self,
        job_id: str,
        *,
        delay: float = 0.1,
        error: Optional[str] = None,
    ) -> Optional[Job]:
        """Return a claimed job to the queue without counting a failure.

        A continuation is a new bounded reasoning slice of the same task, not
        a replay of a failed side-effecting job.  The task-run store owns the
        continuation budget; this queue only provides crash-safe delivery.
        """
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, last_error = ?, next_retry_at = ?,
                        lease_owner = NULL, lease_until = NULL,
                        heartbeat_at = NULL, finished_at = NULL, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        QUEUED,
                        error,
                        now + max(0.0, float(delay)),
                        now,
                        str(job_id),
                        RUNNING,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (str(job_id),)
                ).fetchone()
                return _row_to_job(row) if row else None
            finally:
                conn.close()

    def fail_or_retry(self, job_id: str, error: str) -> Optional[Job]:
        """Retry only transient failures that have not started irreversible work."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    return None
                if row["status"] in TERMINAL_STATES:
                    return _row_to_job(row)
                attempt = int(row["attempt"] or 0)
                max_attempts = int(row["max_attempts"] or 3)
                side_effects = bool(row["side_effects"])
                transient = is_transient_error(error)
                retryable = (not side_effects) and transient and attempt < max_attempts
                if retryable:
                    delay = min(60.0, 2.0 ** max(0, attempt - 1))
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, last_error = ?, next_retry_at = ?,
                            lease_owner = NULL, lease_until = NULL,
                            heartbeat_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (QUEUED, error, now + delay, now, job_id),
                    )
                else:
                    reason = error
                    if side_effects and transient:
                        reason = (
                            f"{error} (not retried: irreversible side effects already ran)"
                        )
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, last_error = ?, finished_at = ?,
                            lease_owner = NULL, lease_until = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (FAILED, reason, now, now, job_id),
                    )
                updated = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                return _row_to_job(updated) if updated else None
            finally:
                conn.close()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return _row_to_job(row) if row else None
            finally:
                conn.close()

    def list_jobs(
        self,
        *,
        statuses: Optional[Iterable[str]] = None,
        limit: int = 100,
    ) -> List[Job]:
        with self._lock:
            conn = self._connect()
            try:
                if statuses:
                    placeholders = ",".join("?" for _ in statuses)
                    rows = conn.execute(
                        f"SELECT * FROM jobs WHERE status IN ({placeholders}) "
                        "ORDER BY created_at DESC LIMIT ?",
                        (*statuses, int(limit)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                        (int(limit),),
                    ).fetchall()
                return [_row_to_job(row) for row in rows]
            finally:
                conn.close()


_instance: Optional[DurableJobQueue] = None
_instance_lock = threading.Lock()


def get_job_queue(db_path: Optional[Path | str] = None) -> DurableJobQueue:
    """Return the process-wide queue, creating it on first use."""
    global _instance
    with _instance_lock:
        if _instance is None:
            if db_path is None:
                from core.runtime_paths import get_data_dir

                db_path = get_data_dir() / "jobs.sqlite"
            _instance = DurableJobQueue(db_path)
        return _instance


def reset_job_queue() -> None:
    global _instance
    with _instance_lock:
        _instance = None


def persist_user_inbound(
    msg: InboundMessage,
    *,
    kind: str = "chat",
    queue: Optional[DurableJobQueue] = None,
) -> InboundMessage:
    """Persist a live user turn before the agent runs.

    Scheduled work stays unattended. Companion/web/Discord chats are durable
    but remain confirmation-gated after resume.
    """
    metadata = dict(msg.metadata or {})
    if metadata.get("durable_job_id"):
        return msg
    if metadata.get("is_confirmation"):
        return msg
    content = str(getattr(msg, "content", "") or "").strip()
    if not content:
        return msg
    store = queue if queue is not None else get_job_queue()
    store.enqueue(
        msg,
        kind=kind,
        unattended=bool(metadata.get("unattended") or metadata.get("is_scheduler")),
    )
    return msg
