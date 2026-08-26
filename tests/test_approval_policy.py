import asyncio
import unittest
from types import SimpleNamespace

from core.loop import AgentLoop


def make_loop(profile="manual"):
    loop = AgentLoop.__new__(AgentLoop)
    loop.config = SimpleNamespace(approval_policy_profile=profile)
    loop.session_whitelists = {}
    loop.pending_confirmations = {}
    return loop


class TestApprovalPolicyDecisions(unittest.TestCase):
    def test_manual_requires_confirmation_by_default(self):
        decision = make_loop()._get_tool_approval_decision("web_one", "run_command")

        self.assertFalse(decision["allowed"])
        self.assertTrue(decision["requires_confirmation"])
        self.assertEqual(decision["reason"], "manual_required")
        self.assertEqual(decision["policy_profile"], "manual")

    def test_session_whitelist_allows_a_previously_approved_tool(self):
        loop = make_loop("session")
        loop.session_whitelists["web_one"] = {"write_file"}

        decision = loop._get_tool_approval_decision("web_one", "write_file")

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "session_whitelist")

    def test_run_command_whitelist_is_scoped_to_the_binary(self):
        loop = make_loop("session")
        # User approved "always allow" for a `git status` invocation.
        loop.session_whitelists["web_one"] = {"run_command::git"}

        git_decision = loop._get_tool_approval_decision(
            "web_one",
            "run_command",
            function_args={"command": "git log --oneline"},
        )
        self.assertTrue(git_decision["allowed"])
        self.assertEqual(git_decision["reason"], "session_whitelist")

        # A different binary (rm) must NOT be unlocked by the git approval.
        rm_decision = loop._get_tool_approval_decision(
            "web_one",
            "run_command",
            function_args={"command": "rm -rf important"},
        )
        self.assertFalse(rm_decision["allowed"])
        self.assertTrue(rm_decision["requires_confirmation"])
        self.assertEqual(rm_decision["reason"], "manual_required")

    def test_session_whitelist_key_scopes_run_command_by_binary(self):
        self.assertEqual(
            AgentLoop._session_whitelist_key(
                "run_command", {"command": "git status --short"}
            ),
            "run_command::git",
        )
        self.assertEqual(
            AgentLoop._session_whitelist_key("write_file", {"path": "a.txt"}),
            "write_file",
        )

    def test_review_ignores_session_whitelist(self):
        loop = make_loop("review")
        loop.session_whitelists["web_one"] = {"write_file"}

        decision = loop._get_tool_approval_decision("web_one", "write_file")

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "manual_required")

    def test_autonomous_and_internal_tools_are_allowed_with_distinct_reasons(self):
        autonomous = make_loop("autonomous")._get_tool_approval_decision(
            "web_one", "delete_file"
        )
        internal = make_loop()._get_tool_approval_decision(
            "system", "run_command", is_internal=True
        )

        self.assertEqual(autonomous["reason"], "policy_autonomous")
        self.assertEqual(internal["reason"], "internal")
        self.assertTrue(autonomous["allowed"])
        self.assertTrue(internal["allowed"])

    def test_whatsapp_uses_fast_autonomous_policy_for_sensitive_tools(self):
        # WhatsApp is intentionally fast: contact allow-listing and hard tool
        # safety checks remain active, but no interactive approval is required.
        for tool in ("run_command", "write_file", "delete_file", "edit_skill"):
            decision = make_loop()._get_tool_approval_decision(
                "whatsapp_one", tool, is_whatsapp=True
            )
            self.assertTrue(decision["allowed"], tool)
            self.assertFalse(decision["requires_confirmation"], tool)
            self.assertEqual(decision["reason"], "channel_whatsapp_autonomous", tool)

        # The same policy applies to every sensitive tool, including cron removal.
        cron = make_loop()._get_tool_approval_decision(
            "whatsapp_one", "cron_remove", is_whatsapp=True
        )
        self.assertTrue(cron["allowed"])
        self.assertFalse(cron["requires_confirmation"])
        self.assertEqual(cron["reason"], "channel_whatsapp_autonomous")

        # Read-only tools remain allowed as before.
        read_only = make_loop()._get_tool_approval_decision(
            "whatsapp_one", "read_file", is_whatsapp=True
        )
        self.assertTrue(read_only["allowed"])
        self.assertEqual(read_only["reason"], "channel_whatsapp_autonomous")

    def test_audit_preview_never_contains_commands_or_file_content(self):
        safe = AgentLoop._approval_audit_preview(
            {
                "kind": "run_command",
                "command": "curl https://example.test/?token=secret",
                "content_preview": "super-secret-content",
                "risk_flags": ["network_access"],
                "affected_paths": ["one", "two"],
            }
        )

        self.assertEqual(
            safe,
            {
                "kind": "run_command",
                "risk_flags": ["network_access"],
                "affected_path_count": 2,
            },
        )

    def test_sensitive_tool_set_covers_every_state_changing_approval_tool(self):
        from core.confirmation import SENSITIVE_TOOLS

        self.assertTrue(
            {
                "write_file",
                "delete_file",
                "run_command",
                "cron_remove",
                "edit_skill",
            }.issubset(
                SENSITIVE_TOOLS
            )
        )


class TestApprovalAuditEvents(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_tool_audits_approval_and_adds_session_whitelist(self):
        loop = make_loop("session")
        events = []
        loop._log_session_event = lambda session_key, event: events.append(
            (session_key, event)
        )
        signal = asyncio.Event()
        loop.pending_confirmations["conf_one"] = {
            "event": signal,
            "approved": False,
            "session_key": "web_one",
            "tool": "write_file",
            "policy_profile": "session",
        }

        result = await loop.confirm_tool(
            "conf_one", True, session_whitelist=True, source="extension"
        )

        self.assertTrue(result)
        self.assertTrue(signal.is_set())
        self.assertIn("write_file", loop.session_whitelists["web_one"])
        self.assertEqual(events[0][1]["type"], "approval_decided")
        self.assertEqual(events[0][1]["client_source"], "extension")
        self.assertTrue(events[0][1]["session_whitelist"])

    async def test_review_approval_does_not_create_a_session_whitelist(self):
        loop = make_loop("review")
        events = []
        loop._log_session_event = lambda session_key, event: events.append(event)
        loop.pending_confirmations["conf_review"] = {
            "event": asyncio.Event(),
            "approved": False,
            "session_key": "web_one",
            "tool": "run_command",
            "policy_profile": "review",
        }

        await loop.confirm_tool(
            "conf_review", True, session_whitelist=True, source="not-a-client"
        )

        self.assertNotIn("web_one", loop.session_whitelists)
        self.assertFalse(events[0]["session_whitelist"])
        self.assertEqual(events[0]["client_source"], "api")

    async def test_denial_is_audited_without_arguments(self):
        loop = make_loop()
        events = []
        loop._log_session_event = lambda session_key, event: events.append(event)
        loop.pending_confirmations["conf_deny"] = {
            "event": asyncio.Event(),
            "approved": False,
            "session_key": "web_one",
            "tool": "delete_file",
            "policy_profile": "manual",
        }

        await loop.confirm_tool("conf_deny", False, source="web")

        self.assertEqual(events[0]["decision_reason"], "user_denied")
        self.assertNotIn("args", events[0])
        self.assertNotIn("preview", events[0])
