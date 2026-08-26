import sys
import types
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

if "loguru" not in sys.modules:
    loguru = types.ModuleType("loguru")

    class _DummyLogger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    loguru.logger = _DummyLogger()
    sys.modules["loguru"] = loguru

try:
    from litellm import AuthenticationError
except Exception:
    class AuthenticationError(Exception):
        def __init__(self, *args, **kwargs):
            message = kwargs.get("message")
            if message is None and args:
                message = args[0]
            super().__init__(message or "")


class TestLlmFallbacks(unittest.IsolatedAsyncioTestCase):
    async def test_codex_error_payload_is_failover_worthy(self):
        from core.loop import AgentLoop

        self.assertTrue(
            AgentLoop._should_failover_model(
                RuntimeError(
                    "Codex provider returned an error for gpt-5.4-mini with no visible response."
                )
            )
        )

    async def test_llm_call_switches_to_fallback_model_after_auth_failure(self):
        from core.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop.model = "xai/grok-4-fast-reasoning"
        loop.fallback_models = ["gemini/gemini-2.0-flash"]
        loop.config = SimpleNamespace(llm=SimpleNamespace(base_url=None))
        loop._sanitize_messages_for_llm = lambda messages, session_key: messages
        loop._get_tool_definitions_for_turn = lambda text: []
        loop._tool_definition_names = lambda tools: []
        loop._log_tool_debug = lambda *args, **kwargs: None
        loop._with_trace_metadata = lambda meta, **kwargs: meta
        loop._should_retry_without_images = lambda e, messages: False
        loop._downgrade_image_messages_for_text_model = lambda messages, session_key: False
        loop._disable_image_inputs_for_session = lambda session_key: None
        loop.bus = SimpleNamespace(publish_outbound=AsyncMock())

        response = object()
        auth_error = AuthenticationError(
            message="Incorrect API key provided",
            llm_provider="xai",
            model="grok-4-fast-reasoning",
        )
        loop.llm_client = SimpleNamespace(
            complete=AsyncMock(side_effect=[auth_error, response]),
        )

        with patch.object(
            loop,
            "_resolve_provider_chain",
            return_value=[
                (
                    "xai/grok-4-fast-reasoning",
                    "grok-4-fast-reasoning",
                    "https://api.x.ai/v1",
                    "bad-key",
                    "openai",
                ),
                (
                    "gemini/gemini-2.0-flash",
                    "gemini-2.0-flash",
                    None,
                    "good-key",
                    "gemini",
                ),
            ],
        ):
            result = await loop._llm_call_with_retry(
                messages=[{"role": "user", "content": "hi"}],
                session_key="web_demo",
                msg=None,
                max_retries=3,
                stream=False,
                include_tools=False,
            )

        self.assertIs(result, response)
        mocked = loop.llm_client.complete
        self.assertEqual(mocked.await_count, 2)
        first_provider = mocked.await_args_list[0].args[0]
        second_provider = mocked.await_args_list[1].args[0]
        self.assertEqual(first_provider.model, "grok-4-fast-reasoning")
        self.assertEqual(second_provider.model, "gemini-2.0-flash")
        self.assertEqual(
            loop.model,
            "xai/grok-4-fast-reasoning",
            "fallback selection must not replace the configured primary model",
        )

    async def test_llm_call_retries_same_provider_after_transient_failure(self):
        from core.loop import AgentLoop

        class TransientProviderError(Exception):
            pass

        loop = AgentLoop.__new__(AgentLoop)
        loop.model = "openai/gpt-4o-mini"
        loop.fallback_models = []
        loop.config = SimpleNamespace(
            command_timeout=0,
            llm=SimpleNamespace(base_url=None),
        )
        loop._sanitize_messages_for_llm = lambda messages, session_key: messages
        loop._get_tool_definitions_for_turn = lambda text: []
        loop._tool_definition_names = lambda tools: []
        loop._log_tool_debug = lambda *args, **kwargs: None
        loop._should_retry_without_images = lambda e, messages: False
        loop._downgrade_image_messages_for_text_model = lambda messages, session_key: False
        loop._disable_image_inputs_for_session = lambda session_key: None
        loop.bus = SimpleNamespace(publish_outbound=AsyncMock())

        response = object()
        loop.llm_client = SimpleNamespace(
            complete=AsyncMock(
                side_effect=[TransientProviderError("connection reset"), response]
            ),
        )

        with patch.object(
            loop,
            "_resolve_provider_chain",
            return_value=[
                (
                    "openai/gpt-4o-mini",
                    "gpt-4o-mini",
                    None,
                    "openai-key",
                    "openai",
                ),
            ],
        ), patch("core.loop.APIConnectionError", TransientProviderError), patch(
            "core.loop.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            result = await loop._llm_call_with_retry(
                messages=[{"role": "user", "content": "try again"}],
                session_key="web_demo",
                msg=None,
                max_retries=2,
                stream=False,
                include_tools=False,
            )

        self.assertIs(result, response)
        self.assertEqual(loop.llm_client.complete.await_count, 2)
        sleep.assert_awaited_once()

    async def test_llm_call_times_out_instead_of_hanging_turn(self):
        from core.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop.model = "openai-codex/gpt-5.4-mini"
        loop.fallback_models = []
        loop.config = SimpleNamespace(
            command_timeout=0.01,
            llm=SimpleNamespace(base_url=None),
        )
        loop._sanitize_messages_for_llm = lambda messages, session_key: messages
        loop._get_tool_definitions_for_turn = lambda text: []
        loop._tool_definition_names = lambda tools: []
        loop._log_tool_debug = lambda *args, **kwargs: None
        loop._should_retry_without_images = lambda e, messages: False
        loop._downgrade_image_messages_for_text_model = lambda messages, session_key: False
        loop._disable_image_inputs_for_session = lambda session_key: None

        async def never_finishes(*args, **kwargs):
            await asyncio.sleep(1)

        loop.llm_client = SimpleNamespace(complete=AsyncMock(side_effect=never_finishes))

        with patch.object(
            loop,
            "_resolve_provider_chain",
            return_value=[
                (
                    "openai-codex/gpt-5.4-mini",
                    "gpt-5.4-mini",
                    None,
                    None,
                    None,
                ),
            ],
        ):
            with self.assertRaisesRegex(TimeoutError, "AI provider timed out"):
                await loop._llm_call_with_retry(
                    messages=[{"role": "user", "content": "hi"}],
                    session_key="web_demo",
                    msg=None,
                    max_retries=1,
                    stream=False,
                    include_tools=False,
                )

        loop.llm_client.complete.assert_awaited_once()
