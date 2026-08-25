import tempfile
import unittest
from pathlib import Path

from core.events import InboundMessage
from core.job_queue import DurableJobQueue, QUEUED, RUNNING
from core.task_runs import (
    COMPLETED,
    PLANNING,
    RETRYING,
    TaskRunStore,
    derive_acceptance_criteria,
)


class TestTaskRuns(unittest.TestCase):
    def test_task_run_persists_checkpoint_and_resumes_same_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskRunStore(Path(tmp) / "task_runs.sqlite")
            run = store.create_or_get("run-1", "Fix the code and run pytest")
            self.assertIn("changed files", " ".join(run.acceptance_criteria))
            started = store.start(run.run_id, phase=PLANNING)
            self.assertEqual(started.slice_count, 1)
            checkpointed = store.checkpoint(
                run.run_id,
                phase="executing",
                checkpoint={"last_tool": "edit_file"},
                next_action="Run verification",
            )
            resumed = store.request_resume(
                run.run_id,
                phase="verifying",
                next_action="Run verify_files",
                checkpoint=checkpointed.checkpoint,
            )
            reloaded = TaskRunStore(Path(tmp) / "task_runs.sqlite").get("run-1")
            self.assertEqual(resumed.status, RETRYING)
            self.assertEqual(reloaded.run_id, "run-1")
            self.assertEqual(reloaded.checkpoint["last_tool"], "edit_file")
            self.assertEqual(reloaded.next_action, "Run verify_files")
            completed = store.complete(
                run.run_id, result="verified", checkpoint={"verification_state": "passed"}
            )
            self.assertEqual(completed.status, COMPLETED)
            self.assertEqual(completed.result_preview, "verified")

    def test_continuation_requeues_without_counting_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = DurableJobQueue(Path(tmp) / "jobs.sqlite")
            msg = InboundMessage(
                channel="web",
                sender_id="user",
                chat_id="chat",
                content="Continue the task",
            )
            job = queue.enqueue(msg, kind="chat")
            claimed = queue.claim(job.id, "worker")
            self.assertEqual(claimed.status, RUNNING)
            queue.update_payload_metadata(job.id, {"task_run_id": "run-1", "task_run_resume": True})
            queued = queue.requeue_continuation(job.id, delay=0)
            self.assertEqual(queued.status, QUEUED)
            self.assertEqual(queued.attempt, 1)
            self.assertTrue(queued.to_message().metadata["task_run_resume"])

    def test_acceptance_criteria_are_conservative(self):
        criteria = derive_acceptance_criteria("Create a report and send the file")
        self.assertIn("The requested artifact is delivered to the originating channel.", criteria)
        self.assertIn("The original user request is satisfied.", criteria)


if __name__ == "__main__":
    unittest.main()
