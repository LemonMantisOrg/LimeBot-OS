import unittest
from types import SimpleNamespace

from core.events import InboundMessage
from core.loop import AgentLoop
from core.unattended import (
    command_is_allowlisted,
    evaluate_unattended_tool,
    is_unattended_turn,
    path_is_allowlisted,
)


class TestUnattendedPolicy(unittest.TestCase):
    def test_live_chat_is_not_unattended(self):
        msg = InboundMessage("web", "u", "c", "hello")
        self.assertFalse(is_unattended_turn(msg))

    def test_scheduler_and_durable_flags_are_unattended(self):
        cron = InboundMessage(
            "web", "u", "c", "x", metadata={"is_scheduler": True}
        )
        queued = InboundMessage(
            "web", "u", "c", "x", metadata={"durable_job_id": "abc"}
        )
        self.assertTrue(is_unattended_turn(cron))
        self.assertTrue(is_unattended_turn(queued))

    def test_path_and_command_allowlists(self):
        self.assertTrue(path_is_allowlisted("temp/out.txt", ["temp"]))
        self.assertFalse(path_is_allowlisted("/etc/passwd", ["temp"]))
        self.assertTrue(command_is_allowlisted("python script.py", ["python"]))
        self.assertFalse(command_is_allowlisted("rm -rf /", ["python"]))

    def test_unattended_write_is_allowlisted_without_autonomous(self):
        config = SimpleNamespace(
            unattended=SimpleNamespace(
                path_allowlist=["temp"],
                command_allowlist=["python"],
            )
        )
        allowed = evaluate_unattended_tool(
            "write_file", {"path": "temp/ok.txt"}, config
        )
        denied = evaluate_unattended_tool(
            "run_command", {"command": "rm -rf /"}, config
        )
        self.assertTrue(allowed["allowed"])
        self.assertFalse(allowed["requires_confirmation"])
        self.assertEqual(allowed["reason"], "unattended_path_allowlist")
        self.assertFalse(denied["allowed"])
        self.assertFalse(denied["requires_confirmation"])
        self.assertEqual(denied["reason"], "unattended_command_denied")

    def test_loop_uses_unattended_policy_for_scheduled_jobs(self):
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = SimpleNamespace(
            approval_policy_profile="manual",
            unattended=SimpleNamespace(
                path_allowlist=["temp"],
                command_allowlist=["python"],
            ),
        )
        loop.session_whitelists = {}
        msg = InboundMessage(
            "web",
            "scheduler",
            "jobs",
            "[SCHEDULER] write a file",
            metadata={"is_scheduler": True, "durable": True},
        )
        decision = loop._get_tool_approval_decision(
            "web_jobs",
            "write_file",
            function_args={"path": "temp/report.txt", "content": "ok"},
            msg=msg,
        )
        self.assertTrue(decision["allowed"])
        self.assertFalse(decision["requires_confirmation"])
        self.assertIn("unattended", decision["reason"])

        live = loop._get_tool_approval_decision(
            "web_jobs",
            "write_file",
            function_args={"path": "temp/report.txt"},
        )
        self.assertTrue(live["requires_confirmation"])
        self.assertEqual(live["reason"], "manual_required")
