import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.bus import MessageBus
from core.events import InboundMessage
from core.job_queue import DurableJobQueue, persist_user_inbound, reset_job_queue
from channels.base import BaseChannel


class _StubChannel(BaseChannel):
    def __init__(self, bus):
        super().__init__(SimpleNamespace(), bus)

    @property
    def name(self) -> str:
        return "web"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg) -> None:
        return None


class TestChannelDurableChat(unittest.IsolatedAsyncioTestCase):
    async def test_handle_message_persists_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_job_queue()
            queue = DurableJobQueue(Path(tmp) / "jobs.sqlite")
            bus = MessageBus()
            inbound = []

            async def capture(msg):
                inbound.append(msg)

            bus.publish_inbound = capture
            channel = _StubChannel(bus)
            import core.job_queue as job_queue

            original = job_queue.get_job_queue
            job_queue.get_job_queue = lambda: queue
            try:
                await channel._handle_message(
                    "app-user",
                    "app_workspace",
                    "Write the research notes after a crash.",
                    metadata={"source": "app", "workspace_id": "ws1"},
                )
            finally:
                job_queue.get_job_queue = original
                reset_job_queue()

            self.assertEqual(len(inbound), 1)
            job_id = inbound[0].metadata.get("durable_job_id")
            self.assertTrue(job_id)
            stored = queue.get(job_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.kind, "chat")
            self.assertFalse(stored.payload["metadata"]["unattended"])

    def test_confirmation_is_not_enqueued(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = DurableJobQueue(Path(tmp) / "jobs.sqlite")
            msg = InboundMessage(
                channel="web",
                sender_id="user",
                chat_id="chat",
                content="approve",
                metadata={"is_confirmation": True},
            )
            persist_user_inbound(msg, kind="chat", queue=queue)
            self.assertFalse(msg.metadata.get("durable_job_id"))
            self.assertEqual(queue.list_jobs(), [])
