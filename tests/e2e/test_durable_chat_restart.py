"""Companion/web chat inbound survives kill -9 via the durable queue."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.events import InboundMessage
from core.job_queue import SUCCEEDED, DurableJobQueue, persist_user_inbound


ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).resolve().parent / "harness.py"


class TestDurableChatRestartE2E(unittest.TestCase):
    def _prepare_state(self, state: Path) -> None:
        persona = state / "persona"
        persona.mkdir(parents=True, exist_ok=True)
        (state / "data").mkdir(parents=True, exist_ok=True)
        (persona / "SOUL.md").write_text(
            "I am LimeBot. I finish companion chat after a crash.\n" * 6,
            encoding="utf-8",
        )
        (persona / "IDENTITY.md").write_text(
            "**Name:** LimeBot\n**Style:** concise and reliable.\n",
            encoding="utf-8",
        )
        (persona / "MEMORY.md").write_text("notes\n", encoding="utf-8")

    def _env(self, state: Path, *, sleep: str) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "LIMEBOT_STATE_DIR": str(state),
                "LIMEBOT_E2E_READY_FILE": str(state / "ready"),
                "LIMEBOT_TEST_LLM_MODE": "echo",
                "LIMEBOT_TEST_LLM_SLEEP": sleep,
                "LIMEBOT_TEST_LLM_REPLY": "Companion chat resumed after SIGKILL.",
                "LLM_MODEL": "openai/gpt-test",
                "OPENAI_API_KEY": "sk-test-e2e",
                "ENABLE_DISCORD": "false",
                "ENABLE_WHATSAPP": "false",
                "ENABLE_TELEGRAM": "false",
                "APPROVAL_POLICY_PROFILE": "manual",
                "PYTHONPATH": str(ROOT),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    def _start(self, state: Path, sleep: str) -> subprocess.Popen:
        ready = state / "ready"
        if ready.exists():
            ready.unlink()
        proc = subprocess.Popen(
            [sys.executable, str(HARNESS)],
            cwd=str(ROOT),
            env=self._env(state, sleep=sleep),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if ready.exists():
                return proc
            if proc.poll() is not None:
                output = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
                self.fail(f"harness exited early ({proc.returncode}): {output}")
            time.sleep(0.1)
        proc.kill()
        self.fail("harness did not write ready file")

    def _wait_status(self, db: Path, job_id: str, status: str, timeout: float = 20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            queue = DurableJobQueue(db)
            job = queue.get(job_id)
            last = job.status if job else None
            if last == status:
                return job
            time.sleep(0.1)
        self.fail(f"job {job_id} did not reach {status}; last={last}")

    def test_companion_chat_survives_sigkill(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp)
            self._prepare_state(state)
            db = state / "data" / "jobs.sqlite"
            queue = DurableJobQueue(db)
            msg = InboundMessage(
                channel="web",
                sender_id="app-user",
                chat_id="app_workspace",
                content="Finish this companion message after the process dies.",
                metadata={"source": "app", "workspace_id": "deadbeef1234"},
            )
            persist_user_inbound(msg, kind="chat", queue=queue)
            job_id = msg.metadata["durable_job_id"]
            self.assertFalse(msg.metadata.get("unattended"))

            first = self._start(state, sleep="20")
            try:
                self._wait_status(db, job_id, "running", timeout=15)
                os.kill(first.pid, signal.SIGKILL)
                first.wait(timeout=5)
            finally:
                if first.poll() is None:
                    os.kill(first.pid, signal.SIGKILL)
                    first.wait(timeout=5)

            second = self._start(state, sleep="0")
            try:
                finished = self._wait_status(db, job_id, SUCCEEDED, timeout=20)
                self.assertEqual(finished.status, SUCCEEDED)
                replay = finished.to_message()
                self.assertEqual(replay.metadata.get("workspace_id"), "deadbeef1234")
                self.assertFalse(replay.metadata.get("unattended"))
            finally:
                second.send_signal(signal.SIGTERM)
                try:
                    second.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    second.kill()
                    second.wait(timeout=3)
