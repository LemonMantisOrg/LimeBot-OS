"""
TaskTracker - in-memory task registry with bounded JSON persistence.

Tracks all significant work units flowing through the agent loop so the
operator dashboard can answer "what is LimeBot doing right now?" without
reading logs.
"""

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class TaskType(str, Enum):
    INBOUND_MESSAGE = "inbound_message"
    LLM_TURN = "llm_turn"
    TOOL_CALL = "tool_call"
    SUBAGENT_JOB = "subagent_job"
    OUTBOUND_DELIVERY = "outbound_delivery"
    SCHEDULED_JOB = "scheduled_job"
    CONFIRMATION_WAIT = "confirmation_wait"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
_WORKSPACE_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
        "archived",
    }
)
_MAX_HISTORY = 500
_FLUSH_DEBOUNCE_SECONDS = 2.0


@dataclass
class Task:
    task_id: str
    type: str
    status: str = TaskStatus.QUEUED.value
    channel: str = ""
    session_key: str = ""
    chat_id: str = ""
    summary: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    parent_task_id: str = ""
    attempt: int = 1
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceAttempt:
    attempt_id: str
    status: str = TaskStatus.QUEUED.value
    model: str = ""
    summary: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceArtifact:
    artifact_id: str
    kind: str
    title: str
    path: str = ""
    url: str = ""
    created_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskWorkspace:
    workspace_id: str
    title: str
    origin: str
    status: str = TaskStatus.QUEUED.value
    session_key: str = ""
    chat_id: str = ""
    parent_workspace_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    attempts: List[WorkspaceAttempt] = field(default_factory=list)
    artifacts: List[WorkspaceArtifact] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class TaskTracker:
    """Thread-safe, bounded task registry with JSON snapshot persistence."""

    def __init__(self, data_dir: str = "data"):
        self._data_file = Path(data_dir) / "tasks.json"
        self._active: Dict[str, Task] = {}
        self._history: deque[Task] = deque(maxlen=_MAX_HISTORY)
        self._workspaces: Dict[str, TaskWorkspace] = {}
        self._lock = asyncio.Lock()
        self._last_flush: float = 0.0
        self._flush_pending: bool = False
        self._load()

    @staticmethod
    def _coerce_task(item: Dict[str, Any]) -> Task:
        cleaned = {
            key: value
            for key, value in (item or {}).items()
            if key in Task.__dataclass_fields__
        }
        return Task(**cleaned)

    @staticmethod
    def _coerce_attempt(item: Dict[str, Any]) -> WorkspaceAttempt:
        cleaned = {
            key: value
            for key, value in (item or {}).items()
            if key in WorkspaceAttempt.__dataclass_fields__
        }
        return WorkspaceAttempt(**cleaned)

    @staticmethod
    def _coerce_artifact(item: Dict[str, Any]) -> WorkspaceArtifact:
        cleaned = {
            key: value
            for key, value in (item or {}).items()
            if key in WorkspaceArtifact.__dataclass_fields__
        }
        return WorkspaceArtifact(**cleaned)

    @classmethod
    def _coerce_workspace(cls, item: Dict[str, Any]) -> TaskWorkspace:
        cleaned = {
            key: value
            for key, value in (item or {}).items()
            if key in TaskWorkspace.__dataclass_fields__
        }
        cleaned["attempts"] = [
            cls._coerce_attempt(attempt)
            for attempt in cleaned.get("attempts", []) or []
        ]
        cleaned["artifacts"] = [
            cls._coerce_artifact(artifact)
            for artifact in cleaned.get("artifacts", []) or []
        ]
        return TaskWorkspace(**cleaned)

    @classmethod
    def _copy_workspace(cls, workspace: TaskWorkspace) -> TaskWorkspace:
        return cls._coerce_workspace(asdict(workspace))

    def _load(self) -> None:
        if not self._data_file.exists():
            return
        try:
            raw = json.loads(self._data_file.read_text(encoding="utf-8"))
            for item in raw.get("history", []):
                self._history.append(self._coerce_task(item))
            recovered = []
            recovery_time = time.time()
            for item in raw.get("active", []):
                task = self._coerce_task(item)
                if not task.task_id:
                    continue

                # A JSON snapshot cannot resume an in-flight coroutine.  If
                # the previous process died after flushing an active task,
                # leaving it in ``_active`` would make the dashboard report a
                # task as running forever.  Convert that stale projection to
                # one bounded terminal record during startup instead.
                previous_status = task.status
                metadata = dict(task.metadata or {})
                metadata.update(
                    {
                        "recovered_from_restart": True,
                        "previous_status": previous_status,
                    }
                )
                task.status = TaskStatus.FAILED.value
                task.error = "Runtime restarted before task completed."
                task.metadata = metadata
                task.updated_at = recovery_time
                task.completed_at = recovery_time

                # A crash can leave the same stable ID in both arrays.  Keep
                # one terminal projection so retries/cancellation remain
                # idempotent after reload.
                self._history = deque(
                    (row for row in self._history if row.task_id != task.task_id),
                    maxlen=_MAX_HISTORY,
                )
                self._history.append(task)
                recovered.append(task.task_id)
            for item in raw.get("workspaces", []):
                workspace = self._coerce_workspace(item)
                self._workspaces[workspace.workspace_id] = workspace
            logger.info(
                "TaskTracker: loaded "
                f"{len(self._active)} active tasks, "
                f"{len(self._history)} history entries, and "
                f"{len(self._workspaces)} workspaces."
            )
            if recovered:
                logger.warning(
                    "TaskTracker: recovered %d interrupted task(s) after restart: %s",
                    len(recovered),
                    ", ".join(recovered),
                )
                self._flush_sync()
        except Exception as e:
            logger.error(f"TaskTracker: failed to load history: {e}")

    def _flush_sync(self) -> None:
        try:
            self._data_file.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "active": [asdict(task) for task in self._active.values()],
                "history": [asdict(task) for task in self._history],
                "workspaces": [
                    asdict(workspace) for workspace in self._workspaces.values()
                ],
            }
            self._data_file.write_text(
                json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
            )
            self._last_flush = time.time()
        except Exception as e:
            logger.error(f"TaskTracker: flush failed: {e}")

    async def flush(self) -> None:
        """Write the latest snapshot immediately so crash-resume can see it."""
        self._flush_pending = False
        await asyncio.to_thread(self._flush_sync)

    async def _schedule_flush(self) -> None:
        if self._flush_pending:
            return
        now = time.time()
        if now - self._last_flush >= _FLUSH_DEBOUNCE_SECONDS:
            await asyncio.to_thread(self._flush_sync)
            self._flush_pending = False
        else:
            self._flush_pending = True
            delay = _FLUSH_DEBOUNCE_SECONDS - (now - self._last_flush)
            asyncio.get_event_loop().call_later(
                delay, lambda: asyncio.ensure_future(self._do_deferred_flush())
            )

    async def _do_deferred_flush(self) -> None:
        self._flush_pending = False
        await asyncio.to_thread(self._flush_sync)

    async def create_task(
        self,
        task_type: str,
        summary: str,
        channel: str = "",
        session_key: str = "",
        chat_id: str = "",
        parent_task_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        requested_task_id = str(task_id or "").strip()
        async with self._lock:
            task_id = requested_task_id or uuid.uuid4().hex[:12]
            existing = self._active.get(task_id)
            if existing is None:
                existing = next(
                    (row for row in reversed(self._history) if row.task_id == task_id),
                    None,
                )
            if existing is not None:
                # Dispatch/retry code can safely reuse its stable identity.
                return task_id
            now = time.time()
            task = Task(
                task_id=task_id,
                type=task_type,
                status=TaskStatus.QUEUED.value,
                channel=channel,
                session_key=session_key,
                chat_id=chat_id,
                summary=summary,
                created_at=now,
                updated_at=now,
                parent_task_id=parent_task_id,
                metadata=metadata or {},
            )
            self._active[task_id] = task
            await self._schedule_flush()
        return task_id

    async def update_task(
        self,
        task_id: str,
        status: Optional[str] = None,
        summary: Optional[str] = None,
        error: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> Optional[Task]:
        async with self._lock:
            task = self._active.get(task_id)
            if task is None:
                # Terminal records are immutable projections.  Returning the
                # existing snapshot makes completion/failure idempotent when a
                # worker and its cancellation path race each other.
                task = next(
                    (row for row in reversed(self._history) if row.task_id == task_id),
                    None,
                )
                return Task(**asdict(task)) if task is not None else None

            now = time.time()
            if status is not None:
                task.status = status
                if status == TaskStatus.RUNNING.value and task.started_at == 0.0:
                    task.started_at = now
            if summary is not None:
                task.summary = summary
            if error is not None:
                task.error = error
            if metadata_update:
                task.metadata.update(metadata_update)
            task.updated_at = now

            if task.status in {status.value for status in _TERMINAL_STATUSES}:
                task.completed_at = now
                self._active.pop(task_id, None)
                self._history.append(task)

            await self._schedule_flush()
            return Task(**asdict(task))

    async def complete_task(
        self, task_id: str, error: Optional[str] = None
    ) -> Optional[Task]:
        status = TaskStatus.FAILED.value if error else TaskStatus.COMPLETED.value
        return await self.update_task(task_id, status=status, error=error)

    async def cancel_task(self, task_id: str) -> Optional[Task]:
        async with self._lock:
            task = self._active.get(task_id)
            if task is None:
                task = next(
                    (row for row in reversed(self._history) if row.task_id == task_id),
                    None,
                )
                return Task(**asdict(task)) if task is not None else None
            if task.status in _TERMINAL_STATUSES:
                return Task(**asdict(task))
            task.status = TaskStatus.CANCELLED.value
            task.completed_at = time.time()
            task.updated_at = task.completed_at
            self._active.pop(task_id, None)
            self._history.append(task)
            await self._schedule_flush()
            return Task(**asdict(task))

    async def get_task(self, task_id: str) -> Optional[Task]:
        async with self._lock:
            task = self._active.get(task_id)
            if task:
                return Task(**asdict(task))
            for row in reversed(self._history):
                if row.task_id == task_id:
                    return Task(**asdict(row))
        return None

    async def list_tasks(
        self,
        *,
        status_filter: Optional[str] = None,
        type_filter: Optional[str] = None,
        channel_filter: Optional[str] = None,
        session_filter: Optional[str] = None,
        active_only: bool = False,
        failed_only: bool = False,
        limit: int = 100,
    ) -> List[Task]:
        async with self._lock:
            pool: List[Task] = list(self._active.values())
            if not active_only:
                pool.extend(self._history)

            results: List[Task] = []
            for task in pool:
                if status_filter and task.status != status_filter:
                    continue
                if type_filter and task.type != type_filter:
                    continue
                if channel_filter and task.channel != channel_filter:
                    continue
                if session_filter and task.session_key != session_filter:
                    continue
                if failed_only and task.status != TaskStatus.FAILED.value:
                    continue
                results.append(Task(**asdict(task)))

            results.sort(key=lambda item: item.updated_at, reverse=True)
            return results[:limit]

    async def get_active_tasks(self) -> List[Task]:
        async with self._lock:
            return sorted(
                [Task(**asdict(task)) for task in self._active.values()],
                key=lambda item: item.created_at,
                reverse=True,
            )

    async def get_recent_failures(self, limit: int = 20) -> List[Task]:
        async with self._lock:
            failures = [
                Task(**asdict(task))
                for task in self._history
                if task.status == TaskStatus.FAILED.value
            ]
            failures.sort(key=lambda item: item.completed_at, reverse=True)
            return failures[:limit]

    async def create_workspace(
        self,
        title: str,
        origin: str,
        *,
        session_key: str = "",
        chat_id: str = "",
        parent_workspace_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskWorkspace:
        now = time.time()
        workspace = TaskWorkspace(
            workspace_id=uuid.uuid4().hex[:12],
            title=title,
            origin=origin,
            session_key=session_key,
            chat_id=chat_id,
            parent_workspace_id=parent_workspace_id,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        async with self._lock:
            self._workspaces[workspace.workspace_id] = workspace
            await self.flush()
            return self._copy_workspace(workspace)

    async def update_workspace(
        self,
        workspace_id: str,
        *,
        status: Optional[str] = None,
        title: Optional[str] = None,
        session_key: Optional[str] = None,
        chat_id: Optional[str] = None,
        error: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskWorkspace]:
        async with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                return None

            now = time.time()
            if title is not None:
                workspace.title = title
            if session_key is not None:
                workspace.session_key = session_key
            if chat_id is not None:
                workspace.chat_id = chat_id
            if status is not None:
                workspace.status = status
                if status == TaskStatus.RUNNING.value and workspace.started_at == 0.0:
                    workspace.started_at = now
                if status == TaskStatus.RUNNING.value:
                    workspace.completed_at = 0.0
                    workspace.error = ""
                if status in _WORKSPACE_TERMINAL_STATUSES:
                    workspace.completed_at = now
            if error is not None:
                workspace.error = error
            if metadata_update:
                workspace.metadata.update(metadata_update)
            workspace.updated_at = now
            await self._schedule_flush()
            return self._copy_workspace(workspace)

    async def get_workspace(self, workspace_id: str) -> Optional[TaskWorkspace]:
        async with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                return None
            return self._copy_workspace(workspace)

    async def list_workspaces(
        self,
        *,
        status_filter: Optional[str] = None,
        origin_filter: Optional[str] = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> List[TaskWorkspace]:
        async with self._lock:
            results: List[TaskWorkspace] = []
            for workspace in self._workspaces.values():
                if status_filter and workspace.status != status_filter:
                    continue
                if origin_filter and workspace.origin != origin_filter:
                    continue
                if active_only and workspace.status in _WORKSPACE_TERMINAL_STATUSES:
                    continue
                results.append(self._copy_workspace(workspace))

            results.sort(key=lambda item: item.updated_at, reverse=True)
            return results[:limit]

    async def add_workspace_attempt(
        self,
        workspace_id: str,
        *,
        model: str = "",
        summary: str = "",
        status: str = TaskStatus.QUEUED.value,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[WorkspaceAttempt]:
        async with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                return None

            now = time.time()
            attempt = WorkspaceAttempt(
                attempt_id=uuid.uuid4().hex[:12],
                status=status,
                model=model,
                summary=summary,
                created_at=now,
                updated_at=now,
                started_at=now if status == TaskStatus.RUNNING.value else 0.0,
                metadata=metadata or {},
            )
            workspace.attempts.append(attempt)
            workspace.updated_at = now
            if status == TaskStatus.RUNNING.value and workspace.started_at == 0.0:
                workspace.started_at = now
            await self.flush()
            return WorkspaceAttempt(**asdict(attempt))

    async def complete_workspace_attempt(
        self,
        workspace_id: str,
        attempt_id: str,
        *,
        status: str = TaskStatus.COMPLETED.value,
        error: str = "",
    ) -> Optional[WorkspaceAttempt]:
        async with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                return None

            attempt = next(
                (item for item in workspace.attempts if item.attempt_id == attempt_id),
                None,
            )
            if attempt is None:
                return None

            now = time.time()
            attempt.status = status
            attempt.error = error
            attempt.updated_at = now
            attempt.completed_at = now
            if attempt.started_at == 0.0:
                attempt.started_at = now
            workspace.updated_at = now
            await self._schedule_flush()
            return WorkspaceAttempt(**asdict(attempt))

    async def add_workspace_artifact(
        self,
        workspace_id: str,
        *,
        kind: str,
        title: str,
        path: str = "",
        url: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[WorkspaceArtifact]:
        async with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                return None

            artifact = WorkspaceArtifact(
                artifact_id=uuid.uuid4().hex[:12],
                kind=kind,
                title=title,
                path=path,
                url=url,
                created_at=time.time(),
                metadata=metadata or {},
            )
            workspace.artifacts.append(artifact)
            workspace.updated_at = time.time()
            await self._schedule_flush()
            return WorkspaceArtifact(**asdict(artifact))

    async def update_workspace_artifact(
        self,
        workspace_id: str,
        artifact_id: str,
        *,
        title: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> Optional[WorkspaceArtifact]:
        """Refresh a bounded workspace artifact without replacing its identity."""
        async with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                return None
            artifact = next(
                (item for item in workspace.artifacts if item.artifact_id == artifact_id),
                None,
            )
            if artifact is None:
                return None
            if title is not None:
                artifact.title = title
            if metadata_update:
                artifact.metadata.update(metadata_update)
            workspace.updated_at = time.time()
            await self._schedule_flush()
            return WorkspaceArtifact(**asdict(artifact))


_instance: Optional[TaskTracker] = None


def get_task_tracker() -> TaskTracker:
    global _instance
    if _instance is None:
        _instance = TaskTracker()
    return _instance
