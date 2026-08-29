import hashlib
import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.operator_invariants import (
    MISSING_VERIFY_PREFIX,
    UNAPPLIED_CHANGESET_PREFIX,
    collect_subagent_failures,
    ensure_visible_failures,
    evaluate_operator_invariants,
    format_subagent_failure_block,
    has_verification_evidence,
    missing_verify_text,
    unapplied_changeset_text,
)


def _outcome(tool, success, text="", verification_status=None):
    return SimpleNamespace(
        tool=tool,
        success=success,
        diagnostic_head=text,
        diagnostic_tail=text,
        verification_status=verification_status,
        verification_detail=text,
    )


class TestOperatorInvariantHelpers(unittest.TestCase):
    def test_mutating_turn_without_verify_is_not_ok(self):
        verdict = evaluate_operator_invariants(
            coding_turn=True,
            coding_goal=True,
            outcomes=[
                _outcome(
                    "write_file",
                    True,
                    "Successfully wrote to 'clamp.py'.",
                )
            ],
        )
        self.assertTrue(verdict.applies)
        self.assertFalse(verdict.ok)
        self.assertTrue(verdict.missing_verify)
        self.assertIn(MISSING_VERIFY_PREFIX, verdict.visible_texts[0])
        self.assertIn("clamp.py", verdict.visible_texts[0])

    def test_verify_files_or_proof_command_satisfies_verify(self):
        mutated = [_outcome("write_file", True, "Successfully wrote to 'clamp.py'.")]
        verified = mutated + [
            _outcome(
                "verify_files",
                True,
                json.dumps({"status": "passed", "paths": ["clamp.py"]}),
                verification_status="passed",
            )
        ]
        self.assertTrue(has_verification_evidence(verified, mutated_paths=["clamp.py"]))
        self.assertTrue(
            evaluate_operator_invariants(
                coding_turn=True, outcomes=verified
            ).ok
        )
        proved = mutated + [
            _outcome(
                "run_command",
                True,
                "python -m unittest\n\nExit Code: 0",
                verification_status="passed",
            )
        ]
        self.assertTrue(has_verification_evidence(proved, mutated_paths=["clamp.py"]))

    def test_unapplied_changeset_is_named_failure(self):
        workspace = SimpleNamespace(
            workspace_id="abc123def456",
            label="web_chat_sub_aa11",
            last_capture={"status": "changed", "changed_files": [{"path": "clamp.py"}]},
        )
        verdict = evaluate_operator_invariants(
            coding_turn=True,
            session_key="web_chat",
            outcomes=[
                _outcome(
                    "spawn_agent",
                    True,
                    "Workspace changes (not merged; call apply_workspace_changeset "
                    "with workspace_id=abc123def456)",
                )
            ],
            pending_workspaces=[workspace],
        )
        self.assertTrue(verdict.missing_apply)
        self.assertIn("abc123def456", verdict.visible_texts[0])
        self.assertIn(UNAPPLIED_CHANGESET_PREFIX, verdict.visible_texts[0])

    def test_casual_turn_does_not_require_verify_or_merge(self):
        verdict = evaluate_operator_invariants(
            casual_turn=True,
            coding_turn=False,
            coding_goal=False,
            outcomes=[],
        )
        self.assertFalse(verdict.applies)
        self.assertTrue(verdict.ok)

    def test_no_silent_fail_appends_exact_text(self):
        failure = missing_verify_text(["clamp.py"])
        visible = ensure_visible_failures("", required_texts=[failure])
        self.assertEqual(visible, failure)
        self.assertIn(MISSING_VERIFY_PREFIX, visible)

    def test_subagent_child_failures_are_formatted_for_parent(self):
        history = [
            {"role": "user", "content": "Task: fix it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "run_command",
                            "arguments": '{"command":"pytest"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "run_command",
                "content": "Error: assertion failed\nExit Code: 1",
            },
        ]
        failures = collect_subagent_failures(history)
        self.assertTrue(failures)
        self.assertIn("assertion failed", failures[0])
        block = format_subagent_failure_block(failures)
        self.assertIn("Child tool failure(s):", block)
        self.assertIn("assertion failed", block)


class TestOperatorInvariantLoop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")

    async def _collect_replies(self, bus, sender_id):
        outbound = []
        while not bus.outbound.empty():
            outbound.append(await bus.consume_outbound())
        return [
            item
            for item in outbound
            if item.metadata.get("reply_to") == sender_id and item.content
        ]

    def _agent(self, bus, consume_plan, tools=None):
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._consume_calls = 0
                self._consume_plan = consume_plan
                self._tool_impls = tools or {}

            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

            def _get_tool_approval_decision(self, *args, **kwargs):
                return {
                    "allowed": True,
                    "requires_confirmation": False,
                    "reason": "test",
                    "policy_profile": "autonomous",
                }

            async def _llm_call_with_retry(self, *args, **kwargs):
                return object()

            async def _consume_stream(self, *args, **kwargs):
                step = self._consume_plan[min(self._consume_calls, len(self._consume_plan) - 1)]
                self._consume_calls += 1
                return step

            async def _execute_tool(self, function_name, function_args, session_key):
                handler = self._tool_impls.get(function_name)
                if handler is not None:
                    return handler(function_args)
                return f"ok:{function_name}"

            async def _build_full_system_prompt(self, *args, **kwargs):
                return "SYSTEM: TEST"

            async def _trim_history(self, *args, **kwargs):
                return

            async def _schedule_task_run_continuation(self, *args, **kwargs):
                return False

        return _TestAgentLoop(bus=bus)

    async def test_mutating_turn_without_verify_cannot_complete_as_success(self):
        from core.bus import MessageBus
        from core.events import InboundMessage
        from core.task_runs import BLOCKED, COMPLETED

        bus = MessageBus()
        agent = self._agent(
            bus,
            [
                (
                    "",
                    [
                        {
                            "id": "w1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path":"clamp.py","content":"x=1\\n"}',
                            },
                        }
                    ],
                    None,
                    False,
                ),
                ("All done, Ready.", [], None, False),
            ],
            tools={"write_file": lambda args: f"Successfully wrote to '{args.get('path')}'."},
        )
        msg = InboundMessage(
            channel="web",
            sender_id="op-user",
            chat_id="verify-gate",
            content="Fix clamp.py and write the clamp helper",
            metadata={},
        )
        await agent._process_message(msg)
        replies = await self._collect_replies(bus, msg.sender_id)
        self.assertTrue(replies, "Missing verify must not end the turn silently.")
        combined = "\n".join(item.content for item in replies)
        self.assertIn(MISSING_VERIFY_PREFIX, combined)
        self.assertIn("clamp.py", combined)
        run = agent.task_runs.get(msg.metadata.get("task_run_id") or "")
        if run is None:
            runs = agent.task_runs.list_runs(limit=20)
            run = next((item for item in runs if "clamp" in item.goal), None)
        self.assertIsNotNone(run)
        self.assertNotEqual(run.status, COMPLETED)
        self.assertEqual(run.status, BLOCKED)
        self.assertNotEqual(replies[-1].metadata.get("turn_status"), "completed")

    async def test_unapplied_isolated_changeset_is_named_failure(self):
        from core.bus import MessageBus
        from core.events import InboundMessage
        from core.task_runs import COMPLETED
        from core.workspace_isolation import IsolatedWorkspace

        source_root = Path("temp") / "operator_invariant_merge"
        source_root.mkdir(parents=True, exist_ok=True)
        live = source_root / "clamp.py"
        live.write_text("def clamp(n):\n    return n\n", encoding="utf-8")
        workspace = await IsolatedWorkspace.create(source_root, label="web_merge-gate")
        try:
            (workspace.root / "clamp.py").write_text(
                "def clamp(n):\n    return max(0, min(n, 10))\n",
                encoding="utf-8",
            )
            capture = await workspace.capture()
            self.assertEqual(capture["status"], "changed")
            workspace.retain()

            bus = MessageBus()
            report = (
                "--- SUB-AGENT REPORT ---\n"
                f"Workspace changes (not merged; call apply_workspace_changeset "
                f"with workspace_id={workspace.workspace_id})\n"
            )
            agent = self._agent(
                bus,
                [
                    (
                        "",
                        [
                            {
                                "id": "s1",
                                "type": "function",
                                "function": {
                                    "name": "spawn_agent",
                                    "arguments": '{"task":"Implement the clamp fix","isolation":"copy"}',
                                },
                            }
                        ],
                        None,
                        False,
                    ),
                    ("The isolated worker finished. Ready.", [], None, False),
                ],
                tools={"spawn_agent": lambda args: report},
            )
            msg = InboundMessage(
                channel="web",
                sender_id="op-user",
                chat_id="merge-gate",
                content="Implement the clamp fix with an isolated sub-agent",
                metadata={},
            )
            await agent._process_message(msg)
            replies = await self._collect_replies(bus, msg.sender_id)
            self.assertTrue(replies, "Unapplied changeset must not end the turn silently.")
            combined = "\n".join(item.content for item in replies)
            self.assertIn(UNAPPLIED_CHANGESET_PREFIX, combined)
            self.assertIn(workspace.workspace_id, combined)
            self.assertEqual(
                live.read_text(encoding="utf-8"),
                "def clamp(n):\n    return n\n",
            )
            self.assertTrue(workspace.root.exists())
            self.assertTrue(workspace.is_pending())
            runs = agent.task_runs.list_runs(limit=20)
            run = next((item for item in runs if "clamp" in item.goal), None)
            self.assertIsNotNone(run)
            self.assertNotEqual(run.status, COMPLETED)
        finally:
            await workspace.cleanup()
            shutil.rmtree(source_root, ignore_errors=True)

    async def test_apply_then_verify_matches_live_tree_and_deletes_clone(self):
        from core.bus import MessageBus
        from core.events import InboundMessage
        from core.task_runs import COMPLETED
        from core.tools import Toolbox
        from core.workspace_isolation import IsolatedWorkspace

        source_root = Path("temp") / "operator_invariant_apply"
        source_root.mkdir(parents=True, exist_ok=True)
        live = source_root / "clamp.py"
        live.write_text("def clamp(n):\n    return n\n", encoding="utf-8")
        leftover = Path("temp") / "bakeoff-invariant-isolated"
        leftover.mkdir(parents=True, exist_ok=True)
        (leftover / "stale.txt").write_text("leftover\n", encoding="utf-8")
        workspace = await IsolatedWorkspace.create(source_root, label="web_apply-gate")
        try:
            expected = hashlib.sha256((workspace.root / "clamp.py").read_bytes()).hexdigest()
            toolbox = Toolbox(
                allowed_paths=[str(Path.cwd()), str(source_root)],
                bus=MessageBus(),
                config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
            )
            toolbox.allowed_paths = [Path.cwd().resolve(), source_root.resolve()]
            token = workspace.activate()
            try:
                await toolbox.edit_file(
                    "clamp.py",
                    [{"old_text": "return n", "new_text": "return max(0, min(n, 10))"}],
                    expected,
                )
            finally:
                workspace.deactivate(token)
            capture = await workspace.capture()
            workspace.retain()
            applied = json.loads(
                await toolbox.apply_workspace_changeset(workspace_id=workspace.workspace_id)
            )
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(
                live.read_text(encoding="utf-8"),
                "def clamp(n):\n    return max(0, min(n, 10))\n",
            )
            self.assertFalse(workspace.root.exists())
            self.assertFalse(leftover.exists())

            bus = MessageBus()
            agent = self._agent(
                bus,
                [
                    (
                        "",
                        [
                            {
                                "id": "v1",
                                "type": "function",
                                "function": {
                                    "name": "verify_files",
                                    "arguments": '{"paths":["clamp.py"]}',
                                },
                            }
                        ],
                        None,
                        False,
                    ),
                    ("Verified the applied clamp change.", [], None, False),
                ],
                tools={
                    "verify_files": lambda args: json.dumps(
                        {"status": "passed", "paths": args.get("paths") or ["clamp.py"]}
                    )
                },
            )
            # A follow-up that only verifies an already-applied tree may complete.
            msg = InboundMessage(
                channel="web",
                sender_id="op-user",
                chat_id="apply-gate",
                content="Verify the clamp.py fix after apply",
                metadata={},
            )
            await agent._process_message(msg)
            replies = await self._collect_replies(bus, msg.sender_id)
            self.assertTrue(replies)
            runs = agent.task_runs.list_runs(limit=20)
            run = next((item for item in runs if "clamp" in item.goal), None)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, COMPLETED)
        finally:
            if workspace.root.exists():
                await workspace.cleanup()
            shutil.rmtree(source_root, ignore_errors=True)
            shutil.rmtree(leftover, ignore_errors=True)

    async def test_verify_failure_and_tool_rejection_stay_in_outbound_chat(self):
        from core.bus import MessageBus
        from core.events import InboundMessage

        failed_verify = json.dumps(
            {
                "status": "failed",
                "paths": ["clamp.py"],
                "detail": "syntax error in clamp.py",
            }
        )
        bus = MessageBus()
        agent = self._agent(
            bus,
            [
                (
                    "",
                    [
                        {
                            "id": "w1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path":"clamp.py","content":"def ("}',
                            },
                        },
                        {
                            "id": "v1",
                            "type": "function",
                            "function": {
                                "name": "verify_files",
                                "arguments": '{"paths":["clamp.py"]}',
                            },
                        },
                    ],
                    None,
                    False,
                ),
                ("", [], None, False),
            ],
            tools={
                "write_file": lambda args: f"Successfully wrote to '{args.get('path')}'." ,
                "verify_files": lambda args: failed_verify,
            },
        )
        msg = InboundMessage(
            channel="web",
            sender_id="op-user",
            chat_id="verify-fail",
            content="Edit clamp.py then verify it",
            metadata={},
        )
        await agent._process_message(msg)
        replies = await self._collect_replies(bus, msg.sender_id)
        self.assertTrue(replies, "Ready/IDLE with empty chat after verify failure is a bug.")
        combined = "\n".join(item.content for item in replies)
        self.assertIn("syntax error in clamp.py", combined)
        self.assertNotEqual(replies[-1].metadata.get("turn_status"), "completed")

    async def test_casual_small_talk_completes_without_verify_or_merge(self):
        from core.bus import MessageBus
        from core.events import InboundMessage
        from core.task_runs import COMPLETED

        bus = MessageBus()
        agent = self._agent(
            bus,
            [("hey, I'm here", [], None, False)],
        )
        msg = InboundMessage(
            channel="web",
            sender_id="op-user",
            chat_id="casual-hi",
            content="hi",
            metadata={},
        )
        await agent._process_message(msg)
        replies = await self._collect_replies(bus, msg.sender_id)
        self.assertTrue(replies)
        combined = "\n".join(item.content for item in replies)
        self.assertNotIn(MISSING_VERIFY_PREFIX, combined)
        self.assertNotIn(UNAPPLIED_CHANGESET_PREFIX, combined)
        self.assertIn("hey, I'm here", combined)
        runs = agent.task_runs.list_runs(limit=20)
        run = next((item for item in runs if item.goal == "hi"), None)
        self.assertIsNotNone(run)
        self.assertEqual(run.status, COMPLETED)

    async def test_subagent_report_includes_child_failure(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _SubLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

            async def _llm_call_with_retry(self, *args, **kwargs):
                self._sub_calls = getattr(self, "_sub_calls", 0) + 1

                class _Fn:
                    name = "run_command"
                    arguments = '{"command":"pytest"}'

                class _Call:
                    id = "child-1"
                    function = _Fn()

                class _Msg:
                    def __init__(self, with_tools: bool):
                        self.content = "" if with_tools else "finished"
                        self.tool_calls = [_Call()] if with_tools else None

                    def model_dump(self):
                        payload = {"role": "assistant", "content": self.content}
                        if self.tool_calls:
                            payload["tool_calls"] = [
                                {
                                    "id": "child-1",
                                    "function": {
                                        "name": "run_command",
                                        "arguments": '{"command":"pytest"}',
                                    },
                                }
                            ]
                        return payload

                class _Choice:
                    def __init__(self, with_tools: bool):
                        self.message = _Msg(with_tools)

                class _Resp:
                    def __init__(self, with_tools: bool):
                        self.choices = [_Choice(with_tools)]
                        self.usage = None

                return _Resp(self._sub_calls == 1)

            async def _execute_tool(self, function_name, function_args, session_key):
                return "Error: child assertion failed\nExit Code: 1"

        bus = MessageBus()
        agent = _SubLoop(bus=bus)
        agent.subagent_registry.get_subagent = lambda name: None
        report = await agent.run_subagent(
            "web_parent",
            "web_parent_sub_test",
            "run the failing tests",
        )
        self.assertIn("child assertion failed", report)
        self.assertNotIn("(Silently completed)", report)


if __name__ == "__main__":
    unittest.main()
