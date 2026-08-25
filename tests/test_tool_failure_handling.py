import asyncio
import unittest


class TestToolFailureHandling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")

    async def test_run_command_nonzero_exit_is_error(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

        bus = MessageBus()
        agent = _TestAgentLoop(bus=bus)

        self.assertTrue(
            agent._is_tool_result_error(
                "run_command", "download failed\n\nExit Code: 1"
            )
        )
        self.assertFalse(
            agent._is_tool_result_error("run_command", "done\n\nExit Code: 0")
        )

    async def test_structured_outcome_preserves_failure_tail_and_classification(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

        agent = _TestAgentLoop(bus=MessageBus())
        long_failure = "setup line\n" + ("noise\n" * 800) + "assertion failed\nExit Code: 17"
        failure = agent._build_tool_outcome("run_command", long_failure)
        self.assertFalse(failure.success)
        self.assertEqual(failure.exit_code, 17)
        self.assertIn("setup line", failure.diagnostic_head)
        self.assertIn("Exit Code: 17", failure.diagnostic_tail)
        self.assertTrue(failure.failure_fingerprint)

        timeout = agent._build_tool_outcome("run_command", "Error: [TIMEOUT] [STALL]")
        self.assertTrue(timeout.timed_out)
        self.assertTrue(timeout.stalled)

    async def test_batch_serializes_write_before_verification_command(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
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

            async def _execute_tool(self, name, args, session_key):
                if name == "write_file":
                    self.order.append("write:start")
                    await asyncio.sleep(0)
                    self.written = True
                    self.order.append("write:done")
                    return "written"
                if name == "run_command":
                    self.order.append("verify")
                    return "verified\n\nExit Code: 0" if self.written else "raced\n\nExit Code: 1"
                return "ok"

        agent = _TestAgentLoop(bus=MessageBus())
        agent.order = []
        agent.written = False
        agent.history["web_test"] = []
        await agent._execute_tool_batch(
            [
                {"id": "write", "function": {"name": "write_file", "arguments": '{"path":"a.py","content":"x"}'}},
                {"id": "verify", "function": {"name": "run_command", "arguments": '{"command":"pytest -q"}'}},
            ],
            "web_test",
            None,
            coding_turn=True,
        )
        self.assertEqual(agent.order, ["write:start", "write:done", "verify"])
        self.assertTrue(agent._last_tool_outcomes[-1].success)

    async def test_repeated_verifier_failure_becomes_actionable_blocker(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

        agent = _TestAgentLoop(bus=MessageBus())
        agent.config.tool_recovery_max_attempts = 3
        agent.history["web_test"] = []
        failure = agent._build_tool_outcome("run_command", "failed assertion\nExit Code: 1")
        agent._last_tool_outcomes = [failure]
        attempts, blocked = await agent._queue_coding_recovery(
            "web_test", None, set(), 0
        )
        self.assertEqual(attempts, 1)
        self.assertIsNone(blocked)
        agent._last_tool_outcomes = [failure]
        attempts, blocked = await agent._queue_coding_recovery(
            "web_test", None, {failure.failure_fingerprint}, attempts
        )
        self.assertEqual(attempts, 2)
        self.assertIsNone(blocked)
        agent._last_tool_outcomes = [failure]
        attempts, blocked = await agent._queue_coding_recovery(
            "web_test", None, {failure.failure_fingerprint}, attempts
        )
        self.assertEqual(attempts, 3)
        self.assertIsNone(blocked)
        agent._last_tool_outcomes = [failure]
        _, blocked = await agent._queue_coding_recovery(
            "web_test", None, {failure.failure_fingerprint}, attempts
        )
        self.assertIn("recovery budget", blocked.lower())

    async def test_non_coding_failure_queues_safe_alternative_recovery(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

        agent = _TestAgentLoop(bus=MessageBus())
        agent.config.tool_recovery_max_attempts = 2
        agent.history["web_test"] = []
        failure = agent._build_tool_outcome(
            "browser_navigate", "Error: certificate verification failed"
        )
        agent._last_tool_outcomes = [failure]
        attempts, blocked = await agent._queue_tool_recovery(
            "web_test", None, set(), 0, coding_turn=False
        )
        self.assertEqual(attempts, 1)
        self.assertIsNone(blocked)
        self.assertIn("different safe route or diagnostic", agent.history["web_test"][-1]["content"])

    async def test_repeated_failure_after_successful_edit_allows_repair(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

        agent = _TestAgentLoop(bus=MessageBus())
        agent.history["web_test"] = [
            {
                "role": "tool",
                "name": "run_command",
                "content": "failed assertion\nExit Code: 1",
            },
            {"role": "tool", "name": "write_file", "content": "written"},
            {
                "role": "tool",
                "name": "run_command",
                "content": "failed assertion\nExit Code: 1",
            },
        ]
        failure = agent._build_tool_outcome("run_command", "failed assertion\nExit Code: 1")
        agent._last_tool_outcomes = [failure]
        attempts, blocked = await agent._queue_coding_recovery(
            "web_test", None, {failure.failure_fingerprint}, 1
        )
        self.assertEqual(attempts, 2)
        self.assertIsNone(blocked)

    async def test_tool_failure_with_empty_followup_emits_fallback_reply(self):
        from core.bus import MessageBus
        from core.events import InboundMessage
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._consume_calls = 0
                self.tool_choices = []

            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

            async def _llm_call_with_retry(self, *args, **kwargs):
                self.tool_choices.append(kwargs.get("tool_choice"))
                return object()

            async def _consume_stream(self, *args, **kwargs):
                self._consume_calls += 1
                if self._consume_calls == 1:
                    return (
                        "",
                        [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"missing.txt"}',
                                },
                            }
                        ],
                        None,
                        False,
                    )
                return ("", [], None, False)

            async def _execute_tool(
                self, function_name: str, function_args: dict, session_key: str
            ):
                return "Error: missing file"

            async def _build_full_system_prompt(self, *args, **kwargs):
                return "SYSTEM: TEST"

            async def _trim_history(self, *args, **kwargs):
                return

        bus = MessageBus()
        agent = _TestAgentLoop(bus=bus)

        msg = InboundMessage(
            channel="web",
            sender_id="tool-user",
            chat_id="tool-chat",
            content="run tool",
            metadata={},
        )

        await agent._process_message(msg)

        outbound = []
        while not bus.outbound.empty():
            outbound.append(await bus.consume_outbound())

        replies = [
            m
            for m in outbound
            if m.metadata.get("reply_to") == msg.sender_id and m.content
        ]
        self.assertTrue(replies, "Expected a user-visible fallback reply.")
        self.assertIn("failed", replies[-1].content.lower())
        self.assertIn("required", agent.tool_choices)

    async def test_text_followup_after_tool_failure_is_not_rendered_as_error(self):
        from core.bus import MessageBus
        from core.events import InboundMessage
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._consume_calls = 0

            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

            async def _llm_call_with_retry(self, *args, **kwargs):
                return object()

            async def _consume_stream(self, *args, **kwargs):
                self._consume_calls += 1
                if self._consume_calls == 1:
                    return (
                        "",
                        [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"TimeTrackerMonoRepo"}',
                                },
                            }
                        ],
                        None,
                        False,
                    )
                return (
                    "I can build this, but I cannot currently access the workspace.",
                    [],
                    None,
                    False,
                )

            async def _execute_tool(
                self, function_name: str, function_args: dict, session_key: str
            ):
                return "Error: Access was denied"

            async def _build_full_system_prompt(self, *args, **kwargs):
                return "SYSTEM: TEST"

            async def _trim_history(self, *args, **kwargs):
                return

            async def _schedule_task_run_continuation(self, *args, **kwargs):
                return False

        bus = MessageBus()
        agent = _TestAgentLoop(bus=bus)
        msg = InboundMessage(
            channel="web",
            sender_id="tool-user",
            chat_id="tool-chat",
            content="inspect the workspace",
            metadata={},
        )

        await agent._process_message(msg)

        outbound = []
        while not bus.outbound.empty():
            outbound.append(await bus.consume_outbound())

        replies = [
            item
            for item in outbound
            if item.metadata.get("reply_to") == msg.sender_id and item.content
        ]
        self.assertTrue(replies, "Expected the assistant's text follow-up.")
        self.assertIn("cannot currently access", replies[-1].content)
        self.assertNotEqual(replies[-1].metadata.get("is_error"), True)
