import asyncio
import shutil
import time
import unittest
import uuid
from pathlib import Path


class _TestBus:
    def __init__(self):
        self.messages = []

    async def publish_inbound(self, msg):
        self.messages.append(msg)


class TestScheduler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")

        from core.scheduler import croniter

        if croniter is None:
            raise unittest.SkipTest("Missing dependency (croniter).")

        self.temp_dir = Path("temp") / f"scheduler_test_{uuid.uuid4().hex[:8]}"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_stale_recurring_job_executes_once_and_reschedules(self):
        from core.scheduler import CronManager

        class _TestCronManager(CronManager):
            def __init__(self, bus, data_file):
                self.bus = bus
                self.jobs = []
                self._running = False
                self.data_file = data_file
                self.lock = asyncio.Lock()

        bus = _TestBus()
        scheduler = _TestCronManager(bus, self.temp_dir / "cron.json")
        now = time.time()
        scheduler.jobs = [
            {
                "id": "stale123",
                "trigger": now - 7200,
                "cron_expr": "0 */4 * * *",
                "tz_offset": None,
                "payload": "@reflect_and_distill",
                "context": {
                    "channel": "system",
                    "chat_id": "global_reflection",
                    "sender_id": "maintenance",
                },
                "created_at": now - 10800,
            }
        ]

        task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.sleep(1.2)
        finally:
            await scheduler.stop()
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(
            len(bus.messages),
            1,
            "Stale recurring jobs should execute once after downtime.",
        )
        self.assertGreater(
            scheduler.jobs[0]["trigger"],
            time.time(),
            "Skipped recurring jobs should advance to a future trigger.",
        )
        self.assertEqual(scheduler.job_state["stale123"]["lastStatus"], "running")

    async def test_future_trigger_replays_latest_missed_cron_slot(self):
        from core.scheduler import CronManager

        class _TestCronManager(CronManager):
            def __init__(self, bus, data_file):
                self.bus = bus
                self.jobs = []
                self.job_state = {}
                self._running = False
                self.data_file = data_file
                self.state_file = data_file.with_name("cron_state.json")
                self.runs_dir = data_file.parent / "cron_runs"
                self.lock = asyncio.Lock()

        bus = _TestBus()
        scheduler = _TestCronManager(bus, self.temp_dir / "cron_missed_future.json")
        now = time.time()
        scheduler.jobs = [
            {
                "id": "future123",
                "trigger": now + 3600,
                "cron_expr": "* * * * *",
                "tz_offset": None,
                "payload": "missed slot",
                "context": {
                    "channel": "web",
                    "chat_id": "dashboard",
                    "sender_id": "tester",
                },
                "created_at": now - 3600,
            }
        ]
        scheduler.job_state = {
            "future123": {
                "last_run_at": now - 180,
                "lastStatus": "ok",
            }
        }

        task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.sleep(1.2)
        finally:
            await scheduler.stop()
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(len(bus.messages), 1)
        self.assertIn("missed slot", bus.messages[0].content)
        self.assertGreater(scheduler.jobs[0]["trigger"], time.time())

    async def test_recent_recurring_job_executes_once_and_reschedules(self):
        from core.scheduler import CronManager

        class _TestCronManager(CronManager):
            def __init__(self, bus, data_file):
                self.bus = bus
                self.jobs = []
                self.job_state = {}
                self._running = False
                self.data_file = data_file
                self.state_file = data_file.with_name("cron_state.json")
                self.runs_dir = data_file.parent / "cron_runs"
                self.lock = asyncio.Lock()

        bus = _TestBus()
        scheduler = _TestCronManager(bus, self.temp_dir / "cron_recent.json")
        now = time.time()
        # Keep the next slot outside this test's 1.2s observation window.
        next_minute = (time.gmtime(now).tm_min + 2) % 60
        scheduler.jobs = [
            {
                "id": "recent123",
                "trigger": now - 5,
                "cron_expr": f"{next_minute} * * * *",
                "tz_offset": None,
                "payload": "@reflect_and_distill",
                "context": {
                    "channel": "system",
                    "chat_id": "global_reflection",
                    "sender_id": "maintenance",
                },
                "created_at": now - 120,
            }
        ]

        task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.sleep(1.2)
        finally:
            await scheduler.stop()
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(
            len(bus.messages),
            1,
            "Recently due recurring jobs should still execute once.",
        )
        self.assertGreater(
            scheduler.jobs[0]["trigger"],
            time.time(),
            "Recurring jobs should be rescheduled to a future trigger.",
        )
        self.assertEqual(scheduler.job_state["recent123"]["lastStatus"], "running")
        self.assertIn("lastDurationMs", scheduler.job_state["recent123"])
        self.assertGreater(scheduler.job_state["recent123"]["nextRunAtMs"], int(time.time() * 1000))

        run_file = scheduler.runs_dir / "recent123.jsonl"
        self.assertTrue(run_file.exists())
        run_log = run_file.read_text(encoding="utf-8")
        self.assertIn('"type": "job_enqueued"', run_log)
        self.assertIn('"durationMs"', run_log)
        self.assertNotIn('"type": "job_finished"', run_log)

        await scheduler.mark_run_finished("recent123", status="ok")
        self.assertEqual(scheduler.job_state["recent123"]["lastStatus"], "ok")
        run_log = run_file.read_text(encoding="utf-8")
        self.assertIn('"type": "job_finished"', run_log)

    async def test_inactive_job_does_not_execute_until_resumed(self):
        from core.scheduler import CronManager

        class _TestCronManager(CronManager):
            def __init__(self, bus, data_file):
                self.bus = bus
                self.jobs = []
                self._running = False
                self.data_file = data_file
                self.lock = asyncio.Lock()

        bus = _TestBus()
        scheduler = _TestCronManager(bus, self.temp_dir / "cron_inactive.json")
        now = time.time()
        scheduler.jobs = [
            {
                "id": "paused123",
                "trigger": now - 5,
                "cron_expr": None,
                "tz_offset": None,
                "active": False,
                "payload": "paused reminder",
                "context": {
                    "channel": "web",
                    "chat_id": "dashboard",
                    "sender_id": "tester",
                },
                "created_at": now - 60,
            }
        ]

        task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.sleep(1.2)
            self.assertEqual(
                len(bus.messages),
                0,
                "Paused jobs should not execute while inactive.",
            )

            updated = await scheduler.set_job_active("paused123", True)
            self.assertIsNotNone(updated)
            self.assertTrue(updated["active"])

            await asyncio.sleep(1.2)
        finally:
            await scheduler.stop()
            await asyncio.wait_for(task, timeout=2)

        self.assertEqual(
            len(bus.messages),
            1,
            "Resumed jobs should execute again when due.",
        )

    async def test_loaded_jobs_default_to_active(self):
        from core.scheduler import CronManager

        data_file = self.temp_dir / "cron_load.json"
        data_file.write_text(
            '[{"id":"job1","trigger":123,"cron_expr":null,"tz_offset":null,"payload":"hi","context":{"channel":"web","chat_id":"dashboard"},"created_at":1}]',
            encoding="utf-8",
        )

        scheduler = CronManager(_TestBus())
        scheduler.data_file = data_file
        scheduler.jobs = []
        scheduler._load_jobs()

        self.assertEqual(len(scheduler.jobs), 1)
        self.assertTrue(scheduler.jobs[0]["active"])

    async def test_state_file_is_loaded_and_merged_into_list_jobs(self):
        from core.scheduler import CronManager

        data_file = self.temp_dir / "cron_state_load.json"
        state_file = self.temp_dir / "cron_state.json"
        data_file.write_text(
            '[{"id":"job1","trigger":4102444800,"cron_expr":null,"tz_offset":null,"payload":"hi","context":{"channel":"web","chat_id":"dashboard"},"created_at":1}]',
            encoding="utf-8",
        )
        state_file.write_text(
            '{"version":1,"jobs":{"job1":{"lastStatus":"ok","lastDurationMs":42,"nextRunAtMs":4102444800000}}}',
            encoding="utf-8",
        )

        scheduler = CronManager(_TestBus())
        scheduler.data_file = data_file
        scheduler.state_file = state_file
        scheduler.jobs = []
        scheduler._load_jobs()
        scheduler._load_state()

        jobs = await scheduler.list_jobs()
        self.assertEqual(jobs[0]["state"]["lastStatus"], "ok")
        self.assertEqual(jobs[0]["state"]["lastDurationMs"], 42)

    async def test_add_job_accepts_name_and_timezone(self):
        from core.scheduler import CronManager

        scheduler = CronManager(_TestBus())
        scheduler.data_file = self.temp_dir / "cron_named.json"
        scheduler.state_file = self.temp_dir / "cron_state_named.json"
        scheduler.jobs = []

        job_id = await scheduler.add_job(
            trigger_time=None,
            message="daily thing",
            context={"channel": "web", "chat_id": "dashboard", "sender_id": "tester"},
            cron_expr="0 9 * * *",
            tz="America/El_Salvador",
            name="Daily thing",
        )

        self.assertEqual(scheduler.jobs[0]["id"], job_id)
        self.assertEqual(scheduler.jobs[0]["name"], "Daily thing")
        self.assertEqual(scheduler.jobs[0]["tz"], "America/El_Salvador")
        self.assertGreater(scheduler.jobs[0]["trigger"], time.time())
