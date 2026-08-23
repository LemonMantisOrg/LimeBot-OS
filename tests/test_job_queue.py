import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.events import InboundMessage
from core.job_queue import (
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    DurableJobQueue,
    is_transient_error,
    persist_user_inbound,
)


class TestDurableJobQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.queue = DurableJobQueue(Path(self.tmp.name) / "jobs.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def _msg(self, content="do the work"):
        return InboundMessage(
            channel="web",
            sender_id="tester",
            chat_id="jobs",
            content=content,
            metadata={"durable": True},
        )

    def test_persist_then_resume_after_simulated_crash(self):
        job = self.queue.enqueue(self._msg(), kind="manual")
        self.assertEqual(job.status, QUEUED)
        claimed = self.queue.claim(job.id, "worker-a")
        self.assertEqual(claimed.status, RUNNING)
        self.assertEqual(claimed.attempt, 1)

        restarted = DurableJobQueue(Path(self.tmp.name) / "jobs.sqlite")
        recovered = restarted.recover_interrupted()
        self.assertEqual([item.id for item in recovered], [job.id])
        self.assertEqual(restarted.get(job.id).status, QUEUED)

        claimed_again = restarted.claim(job.id, "worker-b")
        self.assertIsNotNone(claimed_again)
        finished = restarted.finish(job.id, SUCCEEDED)
        self.assertEqual(finished.status, SUCCEEDED)

    def test_expired_lease_is_requeued(self):
        job = self.queue.enqueue(self._msg())
        self.queue.claim(job.id, "worker-a", lease_seconds=1, now=1000.0)
        recovered = self.queue.recover_interrupted(now=1002.0)
        self.assertEqual(recovered[0].id, job.id)
        self.assertEqual(self.queue.get(job.id).status, QUEUED)

    def test_transient_error_retries_until_side_effects(self):
        self.assertTrue(is_transient_error("Provider 503 overloaded"))
        job = self.queue.enqueue(self._msg(), max_attempts=3)
        self.queue.claim(job.id, "w")
        retried = self.queue.fail_or_retry(job.id, "429 rate limit")
        self.assertEqual(retried.status, QUEUED)
        self.assertGreater(retried.next_retry_at or 0, time.time() - 1)

        later = DurableJobQueue(Path(self.tmp.name) / "jobs.sqlite")
        claimed = later.claim(job.id, "w2", now=time.time() + 10)
        later.mark_side_effect(job.id, "write_file")
        failed = later.fail_or_retry(job.id, "503 again")
        self.assertEqual(failed.status, FAILED)
        self.assertIn("irreversible", failed.last_error)

    def test_chat_enqueue_is_durable_but_not_unattended(self):
        msg = InboundMessage(
            channel="web",
            sender_id="user",
            chat_id="app_1",
            content="Write two files then edit one.",
            metadata={"workspace_id": "abc", "source": "app"},
        )
        persist_user_inbound(msg, kind="chat", queue=self.queue)
        job = self.queue.get(msg.metadata["durable_job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job.kind, "chat")
        self.assertFalse(job.payload["metadata"]["unattended"])
        replay = job.to_message()
        self.assertTrue(replay.metadata["durable"])
        self.assertFalse(replay.metadata["unattended"])
        self.assertEqual(replay.metadata["workspace_id"], "abc")

    def test_cron_enqueue_stays_unattended(self):
        msg = InboundMessage(
            channel="web",
            sender_id="cron",
            chat_id="jobs",
            content="scheduled",
            metadata={"is_scheduler": True},
        )
        job = self.queue.enqueue(msg, kind="cron", cron_job_id="cron1")
        self.assertTrue(job.payload["metadata"]["unattended"])
        self.assertTrue(job.to_message().metadata["unattended"])

    def test_non_transient_error_fails_closed(self):
        job = self.queue.enqueue(self._msg())
        self.queue.claim(job.id, "w")
        failed = self.queue.fail_or_retry(job.id, "invalid tool arguments")
        self.assertEqual(failed.status, FAILED)
