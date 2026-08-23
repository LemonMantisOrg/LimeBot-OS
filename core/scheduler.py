"""
CronManager — scheduled tasks and reminders.
Persists jobs to data/cron.json.
Supports one-time (trigger timestamp) and repeating (cron expression) jobs.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from core.events import InboundMessage

try:
    from croniter import croniter
except ImportError:
    croniter = None

# Per-job in-memory dedup: job_id -> last fired unix timestamp.
# Prevents duplicate fires when the bot restarts multiple times within a period.
_job_last_fired: dict[str, float] = {}


class CronManager:
    """Manages scheduled tasks and reminders."""

    def __init__(
        self,
        bus: Any,
        session_manager: Any | None = None,
        job_queue: Any | None = None,
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.job_queue = job_queue
        self.jobs: List[Dict[str, Any]] = []
        self.job_state: Dict[str, Dict[str, Any]] = {}
        self._running = False
        self.data_file = Path("data/cron.json")
        self.state_file = Path("data/cron_state.json")
        self.runs_dir = Path("data/cron_runs")
        self.lock = asyncio.Lock()
        self._load_jobs()
        self._load_state()

    def _load_jobs(self) -> None:
        """Load jobs from disk (sync — called only at init)."""
        if not self.data_file.exists():
            return
        try:
            self.jobs = json.loads(self.data_file.read_text(encoding="utf-8"))

            valid = [
                j
                for j in self.jobs
                if j.get("trigger") is not None or j.get("cron_expr")
            ]
            if len(valid) != len(self.jobs):
                logger.warning(
                    f"Dropped {len(self.jobs) - len(valid)} jobs with null trigger."
                )
                self.jobs = valid
            for job in self.jobs:
                job.setdefault("active", True)
            logger.info(f"Loaded {len(self.jobs)} scheduled job(s).")
        except Exception as e:
            logger.error(f"Error loading jobs: {e}")
            self.jobs = []

    def _load_state(self) -> None:
        """Load per-job runtime state from disk."""
        state_file = getattr(self, "state_file", self.data_file.parent / "cron_state.json")
        if not state_file.exists():
            self.job_state = {}
            return
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("jobs"), dict):
                self.job_state = raw["jobs"]
            elif isinstance(raw, dict):
                self.job_state = raw
            else:
                self.job_state = {}
            logger.info(f"Loaded scheduler state for {len(self.job_state)} job(s).")
        except Exception as e:
            logger.error(f"Error loading cron state: {e}")
            self.job_state = {}

    def _save_jobs(self) -> None:
        """Save jobs to disk (sync — always called while lock is held)."""
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            self.data_file.write_text(json.dumps(self.jobs, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error saving jobs: {e}")

    def _save_state(self) -> None:
        """Persist per-job runtime state separately from job definitions."""
        try:
            if not hasattr(self, "job_state"):
                self.job_state = {}
            state_file = getattr(self, "state_file", self.data_file.parent / "cron_state.json")
            state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "jobs": self.job_state,
                "updated_at": time.time(),
            }
            state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error saving cron state: {e}")

    def _update_job_state(self, job_id: str, **updates: Any) -> Dict[str, Any]:
        """Update in-memory runtime state for a job and return the state."""
        if not hasattr(self, "job_state"):
            self.job_state = {}
        state = dict(self.job_state.get(job_id, {}))
        state.update({k: v for k, v in updates.items() if v is not None})
        state["updated_at"] = time.time()
        self.job_state[job_id] = state
        return state

    def _append_run_event(self, job_id: str, event: Dict[str, Any]) -> None:
        try:
            runs_dir = getattr(self, "runs_dir", self.data_file.parent / "cron_runs")
            runs_dir.mkdir(parents=True, exist_ok=True)
            run_file = runs_dir / f"{job_id}.jsonl"
            payload = dict(event)
            payload.setdefault("timestamp", time.time())
            with run_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str) + "\n")
        except Exception as e:
            logger.error(f"Error writing cron run event for {job_id}: {e}")

    def _last_run_timestamp(self, job_id: str) -> float:
        state = getattr(self, "job_state", {}).get(job_id, {}) or {}
        last_run_ms = state.get("lastRunAtMs")
        candidates = [
            state.get("last_run_at"),
            last_run_ms / 1000 if isinstance(last_run_ms, (int, float)) else None,
            state.get("last_scheduled_trigger"),
        ]
        for value in candidates:
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
        return 0.0

    def _missed_cron_slot(self, job: Dict[str, Any], now: float) -> Optional[float]:
        """Return the latest missed cron slot when persisted next trigger is already future."""
        if not job.get("cron_expr") or job.get("trigger") is None:
            return None
        if float(job.get("trigger") or 0) <= now:
            return None
        if not croniter:
            return None
        try:
            tzinfo = self._resolve_timezone(job.get("tz"), job.get("tz_offset"))
            previous = croniter(
                job["cron_expr"], datetime.fromtimestamp(now, tz=tzinfo)
            ).get_prev(float)
            created_at = float(job.get("created_at") or 0)
            last_run = self._last_run_timestamp(job["id"])
            if previous > created_at and previous > last_run:
                return previous
        except Exception as e:
            logger.warning(f"Unable to detect missed cron slot for {job.get('id')}: {e}")
        return None

    def _next_cron_trigger_after(
        self,
        job: Dict[str, Any],
        scheduled_trigger: float,
        now: float,
    ) -> float:
        tzinfo = self._resolve_timezone(job.get("tz"), job.get("tz_offset"))
        scheduled_dt = datetime.fromtimestamp(scheduled_trigger, tz=tzinfo)
        cron_it = croniter(job["cron_expr"], scheduled_dt)
        next_trigger = cron_it.get_next(float)
        while next_trigger <= now:
            scheduled_dt = datetime.fromtimestamp(next_trigger, tz=tzinfo)
            cron_it = croniter(job["cron_expr"], scheduled_dt)
            next_trigger = cron_it.get_next(float)
        return next_trigger

    @staticmethod
    def _resolve_timezone(tz_name: Optional[str], tz_offset: Optional[int]):
        if tz_name:
            try:
                return ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                logger.warning(f"Unknown scheduler timezone '{tz_name}', falling back to offset/UTC.")
        if tz_offset is not None:
            return timezone(timedelta(minutes=tz_offset))
        return timezone.utc

    async def add_job(
        self,
        trigger_time: Optional[float],
        message: str,
        context: Dict[str, Any],
        cron_expr: Optional[str] = None,
        tz_offset: Optional[int] = None,
        tz: Optional[str] = None,
        name: Optional[str] = None,
    ) -> str:
        """
        Add a new job.

        Args:
            trigger_time: Unix timestamp for the first (or only) execution.
            message:      Content to send/process when the job fires.
            context:      {channel, chat_id, sender_id, ...}
            cron_expr:    Standard cron expression for repeating jobs.
            tz_offset:    Timezone offset in minutes (e.g. -360 for UTC-6).
            tz:           IANA timezone name such as America/El_Salvador.
            name:         Optional human-readable job name.
        """
        async with self.lock:
            if cron_expr and not trigger_time:
                if not croniter:
                    raise ImportError(
                        "croniter library is required for cron expressions."
                    )
                tzinfo = self._resolve_timezone(tz, tz_offset)
                base = datetime.fromtimestamp(time.time(), tz=tzinfo)

                cron_it = croniter(cron_expr, base)
                trigger_time = cron_it.get_next(float)

            job_id = str(uuid.uuid4())[:8]
            job = {
                "id": job_id,
                "trigger": trigger_time,
                "cron_expr": cron_expr,
                "tz_offset": tz_offset,
                "tz": tz,
                "active": True,
                "name": name or self._default_job_name(message),
                "payload": message,
                "context": context,
                "created_at": time.time(),
            }
            self.jobs.append(job)
            self._save_jobs()

            kind = f"repeating '{cron_expr}'" if cron_expr else "one-time"
            logger.info(f"Added {kind} job {job_id}: '{message}' at {trigger_time}")
            return job_id

    @staticmethod
    def _default_job_name(message: str) -> str:
        """Generate a short display name for scheduler UIs and lists."""
        text = " ".join((message or "").split())
        if not text:
            return "Scheduled job"
        if text.startswith("@"):
            return text.split("::", 1)[0].lstrip("@").replace("_", " ").title()
        return text[:60] + ("..." if len(text) > 60 else "")

    async def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID. Returns True if found and removed."""
        async with self.lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j["id"] != job_id]
            if len(self.jobs) < before:
                self._save_jobs()
                logger.info(f"Removed job {job_id}")
                return True
            return False

    async def set_job_active(self, job_id: str, active: bool) -> Optional[Dict[str, Any]]:
        """Pause or resume a job. Returns the updated job if found."""
        async with self.lock:
            for job in self.jobs:
                if job["id"] != job_id:
                    continue
                job["active"] = bool(active)
                self._update_job_state(job_id, active=bool(active))
                self._save_jobs()
                self._save_state()
                logger.info(
                    f"{'Resumed' if active else 'Paused'} job {job_id}"
                )
                return dict(job)
        return None

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """Return a sorted snapshot of all pending jobs."""
        async with self.lock:
            snapshot = []
            for job in self.jobs:
                item = dict(job)
                state = dict(getattr(self, "job_state", {}).get(job["id"], {}))
                if state:
                    item["state"] = state
                snapshot.append(item)
            return sorted(snapshot, key=lambda x: x.get("trigger") or float("inf"))

    async def register_system_jobs(self, channels: list) -> None:
        """
        Register built-in system jobs (e.g., greetings, check-ins).
        'channels' should be a list of dicts: [{'channel': 'discord', 'chat_id': '...'}]
        """
        existing_payloads = {j["payload"] for j in self.jobs}

        for ch in channels:
            chat_id = ch["chat_id"]
            channel = ch["channel"]
            ctx = {
                "channel": channel,
                "chat_id": chat_id,
                "sender_id": ch.get("sender_id", "system"),
            }

            greeting_payload = f"@morning_greeting::{chat_id}"
            if greeting_payload not in existing_payloads:
                await self.add_job(
                    trigger_time=None,
                    message=greeting_payload,
                    context=ctx,
                    cron_expr="0 8 * * *",
                )
                logger.info(f"Registered morning greeting job for {chat_id}")

            checkin_payload = f"@silence_checkin::{chat_id}"
            if checkin_payload not in existing_payloads:
                await self.add_job(
                    trigger_time=None,
                    message=checkin_payload,
                    context=ctx,
                    cron_expr="0 10 * * *",
                )
                logger.info(f"Registered silence check-in job for {chat_id}")

    async def run(self) -> None:
        """Main loop — checks for triggered jobs every second."""
        self._running = True
        logger.info("Scheduler started.")

        while self._running:
            try:
                now = time.time()

                async with self.lock:
                    due: List[Dict[str, Any]] = []
                    for job in self.jobs:
                        if not job.get("active", True) or job.get("trigger") is None:
                            continue
                        if job["trigger"] <= now:
                            due.append(job)
                            continue
                        missed_slot = self._missed_cron_slot(job, now)
                        if missed_slot is not None:
                            job["_missed_trigger"] = missed_slot
                            due.append(job)

                    for job in due:
                        job_id = job["id"]
                        scheduled_trigger = float(
                            job.pop("_missed_trigger", None) or job["trigger"]
                        )
                        job_to_execute = dict(job)
                        job_to_execute["scheduled_trigger"] = scheduled_trigger

                        if job.get("cron_expr"):
                            lag = max(0.0, now - scheduled_trigger)
                            next_trigger = (
                                float(job["trigger"])
                                if float(job["trigger"]) > now
                                else self._next_cron_trigger_after(job, scheduled_trigger, now)
                            )
                            job["trigger"] = next_trigger
                            job_to_execute["next_trigger"] = next_trigger
                            self._update_job_state(
                                job_id,
                                last_scheduled_trigger=scheduled_trigger,
                                lastScheduledTriggerMs=int(scheduled_trigger * 1000),
                                last_lag_seconds=lag,
                                next_trigger=next_trigger,
                                next_run_at=next_trigger,
                                nextRunAtMs=int(next_trigger * 1000),
                            )
                            logger.info(
                                f"Rescheduled job {job_id} → {job['trigger']} "
                                f"(missed lag={lag:.1f}s)"
                            )

                            # ── Dedup: skip if already fired in this same period ──
                            last_fired = _job_last_fired.get(job_id, 0.0)
                            if (now - last_fired) < lag + 1.0:
                                logger.debug(
                                    f"Dedup: skipping duplicate fire for job {job_id} "
                                    f"(last_fired={last_fired:.1f}, lag={lag:.1f}s)"
                                )
                                continue

                        else:
                            self.jobs = [j for j in self.jobs if j["id"] != job_id]

                        _job_last_fired[job_id] = now
                        self._update_job_state(
                            job_id,
                            last_scheduled_trigger=scheduled_trigger,
                            lastScheduledTriggerMs=int(scheduled_trigger * 1000),
                            lastRunAtMs=int(now * 1000),
                            last_run_at=now,
                            last_status="running",
                            lastStatus="running",
                        )
                        asyncio.create_task(self._execute_job(job_to_execute))

                    if due:
                        self._save_jobs()
                        self._save_state()

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                await asyncio.sleep(5)

        logger.info("Scheduler stopped.")

    async def stop(self) -> None:
        """Signal the run loop to exit."""
        self._running = False

    async def _execute_job(self, job: Dict[str, Any]) -> None:
        """Fire a triggered job by publishing an inbound message."""
        from core.task_tracker import get_task_tracker

        tracker = get_task_tracker()
        started_at = time.time()
        job_id = job["id"]
        scheduled_trigger = job.get("scheduled_trigger", job.get("trigger"))
        next_trigger = job.get("next_trigger", job.get("trigger"))
        _task_id = await tracker.create_task(
            task_type="scheduled_job",
            summary=f"Cron job: {job['payload'][:80]}",
            channel=job.get("context", {}).get("channel", "unknown"),
            chat_id=job.get("context", {}).get("chat_id", "unknown"),
            metadata={"job_id": job_id},
        )
        await tracker.update_task(_task_id, status="running")

        logger.info(f"Executing job {job_id}: {job['payload']}")
        await asyncio.to_thread(
            self._append_run_event,
            job_id,
            {
                "type": "job_started",
                "action": "started",
                "status": "running",
                "payload": job["payload"],
                "context": job.get("context", {}),
                "runAtMs": int(started_at * 1000),
                "scheduledRunAtMs": int(scheduled_trigger * 1000) if scheduled_trigger else None,
                "nextRunAtMs": int(next_trigger * 1000) if next_trigger else None,
            },
        )

        context = job.get("context", {})
        metadata = {
            "is_scheduler": True,
            "original_job_id": job["id"],
            "scheduled_trigger": scheduled_trigger,
            "reply_to": context.get("sender_id", "unknown"),
            "unattended": True,
            "durable": True,
            "tracker_task_id": _task_id,
        }
        msg = InboundMessage(
            channel=context.get("channel", "unknown"),
            sender_id=context.get("sender_id", "unknown"),
            chat_id=context.get("chat_id", "unknown"),
            content=f"[SCHEDULER] {job['payload']}",
            metadata=metadata,
        )
        try:
            queue = getattr(self, "job_queue", None)
            if queue is not None:
                durable = queue.enqueue(
                    msg,
                    kind="cron",
                    cron_job_id=job_id,
                )
                metadata["durable_job_id"] = durable.id
                msg.metadata = metadata
            await self.bus.publish_inbound(msg)
            duration_ms = int((time.time() - started_at) * 1000)
            self._update_job_state(
                job_id,
                last_run_at=started_at,
                lastRunAtMs=int(started_at * 1000),
                last_scheduled_trigger=scheduled_trigger,
                lastScheduledTriggerMs=int(scheduled_trigger * 1000) if scheduled_trigger else None,
                last_status="running",
                lastStatus="running",
                last_run_status="running",
                lastRunStatus="running",
                last_duration_ms=duration_ms,
                lastDurationMs=duration_ms,
                next_trigger=next_trigger,
                next_run_at=next_trigger,
                nextRunAtMs=int(next_trigger * 1000) if next_trigger else None,
                enqueued_at=time.time(),
            )
            await asyncio.to_thread(self._save_state)
            await asyncio.to_thread(
                self._append_run_event,
                job_id,
                {
                    "type": "job_enqueued",
                    "action": "enqueued",
                    "status": "running",
                    "payload": job["payload"],
                    "channel": context.get("channel", "unknown"),
                    "chat_id": context.get("chat_id", "unknown"),
                    "runAtMs": int(started_at * 1000),
                    "scheduledRunAtMs": int(scheduled_trigger * 1000) if scheduled_trigger else None,
                    "durationMs": duration_ms,
                    "nextRunAtMs": int(next_trigger * 1000) if next_trigger else None,
                    "durable_job_id": metadata.get("durable_job_id"),
                },
            )
        except Exception as e:
            duration_ms = int((time.time() - started_at) * 1000)
            prior_errors = int(
                getattr(self, "job_state", {}).get(job_id, {}).get(
                    "consecutive_errors",
                    getattr(self, "job_state", {}).get(job_id, {}).get("consecutiveErrors", 0),
                )
                or 0
            )
            state = self._update_job_state(
                job_id,
                last_run_at=started_at,
                lastRunAtMs=int(started_at * 1000),
                last_scheduled_trigger=scheduled_trigger,
                lastScheduledTriggerMs=int(scheduled_trigger * 1000) if scheduled_trigger else None,
                last_status="error",
                lastStatus="error",
                last_run_status="error",
                lastRunStatus="error",
                last_error=str(e),
                lastError=str(e),
                last_duration_ms=duration_ms,
                lastDurationMs=duration_ms,
                next_trigger=next_trigger,
                next_run_at=next_trigger,
                nextRunAtMs=int(next_trigger * 1000) if next_trigger else None,
                consecutive_errors=prior_errors + 1,
                consecutiveErrors=prior_errors + 1,
            )
            await asyncio.to_thread(self._save_state)
            logger.error(f"Failed to publish job {job_id}: {e}")
            await asyncio.to_thread(
                self._append_run_event,
                job_id,
                {
                    "type": "job_error",
                    "action": "finished",
                    "status": "error",
                    "payload": job["payload"],
                    "error": str(e),
                    "runAtMs": int(started_at * 1000),
                    "scheduledRunAtMs": int(scheduled_trigger * 1000) if scheduled_trigger else None,
                    "durationMs": duration_ms,
                    "nextRunAtMs": int(next_trigger * 1000) if next_trigger else None,
                    "state": state,
                },
            )
            await tracker.complete_task(_task_id, error=str(e))

    async def mark_run_finished(
        self,
        job_id: str,
        *,
        status: str = "ok",
        error: Optional[str] = None,
        tracker_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record cron completion only after the agent turn finishes."""
        now = time.time()
        prior = getattr(self, "job_state", {}).get(job_id, {})
        started_at = float(prior.get("last_run_at") or now)
        duration_ms = int((now - started_at) * 1000)
        normalized = "ok" if status in {"ok", "succeeded", "completed"} else "error"
        state = self._update_job_state(
            job_id,
            last_status=normalized,
            lastStatus=normalized,
            last_run_status=normalized,
            lastRunStatus=normalized,
            last_error=error,
            lastError=error,
            last_duration_ms=duration_ms,
            lastDurationMs=duration_ms,
            consecutive_errors=0 if normalized == "ok" else int(prior.get("consecutive_errors") or 0) + 1,
            consecutiveErrors=0 if normalized == "ok" else int(prior.get("consecutiveErrors") or 0) + 1,
        )
        await asyncio.to_thread(self._save_state)
        await asyncio.to_thread(
            self._append_run_event,
            job_id,
            {
                "type": "job_finished" if normalized == "ok" else "job_error",
                "action": "finished",
                "status": normalized,
                "error": error,
                "durationMs": duration_ms,
                "previousStatus": prior.get("last_status") or prior.get("lastStatus"),
                "state": state,
            },
        )
        if tracker_task_id:
            from core.task_tracker import get_task_tracker

            await get_task_tracker().complete_task(
                tracker_task_id, error=error if normalized != "ok" else None
            )
        return state
