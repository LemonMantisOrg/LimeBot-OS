import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.events import InboundMessage
from core.job_queue import SUCCEEDED, DurableJobQueue
from core.scheduler import CronManager


class _RecordingBus:
    def __init__(self):
        self.messages = []

    async def publish_inbound(self, msg):
        self.messages.append(msg)


class TestCronCompletesAfterAgent(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_is_not_completion(self):
        with TemporaryDirectory() as tmp:
            queue = DurableJobQueue(Path(tmp) / "jobs.sqlite")
            bus = _RecordingBus()
            scheduler = CronManager(bus, job_queue=queue)
            scheduler.data_file = Path(tmp) / "cron.json"
            scheduler.state_file = Path(tmp) / "cron_state.json"
            scheduler.runs_dir = Path(tmp) / "cron_runs"
            scheduler.jobs = []
            scheduler.job_state = {}

            job = {
                "id": "cron1",
                "trigger": time.time(),
                "payload": "write the daily note",
                "context": {
                    "channel": "web",
                    "chat_id": "dashboard",
                    "sender_id": "tester",
                },
            }
            await scheduler._execute_job(job)
            self.assertEqual(len(bus.messages), 1)
            self.assertEqual(scheduler.job_state["cron1"]["lastStatus"], "running")
            durable_id = bus.messages[0].metadata["durable_job_id"]
            stored = queue.get(durable_id)
            self.assertEqual(stored.status, "queued")

            queue.claim(durable_id, "agent")
            queue.finish(durable_id, SUCCEEDED)
            await scheduler.mark_run_finished("cron1", status="ok")
            self.assertEqual(scheduler.job_state["cron1"]["lastStatus"], "ok")

    async def test_message_is_unattended_and_durable(self):
        bus = _RecordingBus()
        scheduler = CronManager(bus)
        scheduler.jobs = []
        scheduler.job_state = {}
        await scheduler._execute_job(
            {
                "id": "cron2",
                "trigger": time.time(),
                "payload": "hello",
                "context": {"channel": "web", "chat_id": "c", "sender_id": "s"},
            }
        )
        msg = bus.messages[0]
        self.assertIsInstance(msg, InboundMessage)
        self.assertTrue(msg.metadata["is_scheduler"])
        self.assertTrue(msg.metadata["unattended"])
        self.assertEqual(scheduler.job_state["cron2"]["lastStatus"], "running")
