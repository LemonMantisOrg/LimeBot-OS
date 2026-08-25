import unittest
from unittest.mock import AsyncMock, patch

from core.llm_client import ChatRequest, LimeLLMClient, ProviderConfig


class TestLlmClient(unittest.IsolatedAsyncioTestCase):
    def test_resolve_provider_uses_existing_openrouter_resolution(self):
        client = LimeLLMClient()

        with patch(
            "core.llm_client.resolve_provider_config",
            return_value={
                "model": "anthropic/claude-sonnet-4.6",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "openrouter-secret",
                "custom_llm_provider": "openai",
            },
        ):
            provider = client.resolve_provider(
                "openrouter/anthropic/claude-sonnet-4.6"
            )

        self.assertEqual(provider.source_model, "openrouter/anthropic/claude-sonnet-4.6")
        self.assertEqual(provider.model, "anthropic/claude-sonnet-4.6")
        self.assertEqual(provider.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(provider.api_key, "openrouter-secret")
        self.assertEqual(provider.custom_llm_provider, "openai")
        self.assertFalse(provider.is_codex)

    def test_resolve_provider_marks_codex_models(self):
        client = LimeLLMClient()

        with patch(
            "core.llm_client.resolve_provider_config",
            return_value={
                "model": "gpt-5.4",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_key": "codex-secret",
                "custom_llm_provider": "openai",
            },
        ):
            provider = client.resolve_provider("openai-codex/gpt-5.4")

        self.assertEqual(provider.source_model, "openai-codex/gpt-5.4")
        self.assertEqual(provider.model, "gpt-5.4")
        self.assertEqual(provider.base_url, "https://chatgpt.com/backend-api/codex")
        self.assertEqual(provider.api_key, "codex-secret")
        self.assertEqual(provider.custom_llm_provider, "openai")
        self.assertTrue(provider.is_codex)

    async def test_complete_uses_codex_stream_bridge_for_streaming_requests(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="openai-codex/gpt-5.4",
            model="gpt-5.4",
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="codex-secret",
            custom_llm_provider="openai",
            is_codex=True,
        )
        request = ChatRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            session_id="session-123",
        )
        sentinel = object()

        async def passthrough(func, *args):
            return func(*args)

        with patch("core.llm_client.asyncio.to_thread", side_effect=passthrough), patch(
            "core.llm_client.stream_codex_response",
            return_value=sentinel,
        ) as stream_mock:
            result = await client.complete(provider, request)

        self.assertIs(result, sentinel)
        stream_mock.assert_called_once_with(
            "openai-codex/gpt-5.4",
            [{"role": "user", "content": "hi"}],
            None,
            "session-123",
            "auto",
        )

    async def test_complete_uses_litellm_with_tools_when_present(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="openai/gpt-4o",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="openai-secret",
            custom_llm_provider=None,
            is_codex=False,
        )
        request = ChatRequest(
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "list_dir",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            max_tokens=32,
        )
        response = object()

        with patch("core.llm_client.acompletion", new=AsyncMock(return_value=response)) as mock_completion:
            result = await client.complete(provider, request)

        self.assertIs(result, response)
        kwargs = mock_completion.await_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o")
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(kwargs["tools"], request.tools)
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertEqual(kwargs["max_tokens"], 32)
        self.assertFalse(kwargs["stream"])
        self.assertNotIn("stream_options", kwargs)

    async def test_complete_retries_tool_call_without_reasoning_effort(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="openai/gpt-5.6-luna",
            model="gpt-5.6-luna",
            base_url="https://api.openai.com/v1",
            api_key="openai-secret",
            custom_llm_provider="openai",
            is_codex=False,
        )
        request = ChatRequest(
            messages=[{"role": "user", "content": "check GitHub"}],
            tools=[{"type": "function", "function": {"name": "list_repos"}}],
        )
        response = object()
        provider_error = Exception(
            "Function tools with reasoning_effort are not supported for "
            "gpt-5.6-luna in /v1/chat/completions. To use function tools, "
            "use /v1/responses or set reasoning_effort to 'none'."
        )

        with patch(
            "core.llm_client.acompletion",
            new=AsyncMock(side_effect=[provider_error, response]),
        ) as mock_completion:
            result = await client.complete(provider, request)

        self.assertIs(result, response)
        self.assertEqual(mock_completion.await_count, 2)
        self.assertNotIn("reasoning_effort", mock_completion.await_args_list[0].kwargs)
        self.assertEqual(
            mock_completion.await_args_list[1].kwargs["reasoning_effort"], "none"
        )

    async def test_complete_bounds_long_tool_call_ids_and_preserves_links(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="openai/gpt-4o",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="openai-secret",
            custom_llm_provider=None,
            is_codex=False,
        )
        long_id = "call_" + ("provider-generated-segment-" * 4)
        request = ChatRequest(
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": long_id,
                            "type": "function",
                            "function": {"name": "list_dir", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": long_id,
                    "name": "list_dir",
                    "content": "README.md",
                },
            ]
        )

        with patch(
            "core.llm_client.acompletion", new=AsyncMock(return_value=object())
        ) as mock_completion:
            await client.complete(provider, request)

        sent_messages = mock_completion.await_args.kwargs["messages"]
        sent_call_id = sent_messages[0]["tool_calls"][0]["id"]
        self.assertEqual(len(sent_call_id), 64)
        self.assertTrue(sent_call_id.startswith("call_"))
        self.assertEqual(sent_messages[1]["tool_call_id"], sent_call_id)
        self.assertEqual(request.messages[0]["tool_calls"][0]["id"], long_id)
        self.assertEqual(request.messages[1]["tool_call_id"], long_id)

    async def test_complete_keeps_distinct_long_tool_call_ids_unique(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="openai/gpt-4o",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="openai-secret",
            custom_llm_provider=None,
            is_codex=False,
        )
        common_prefix = "call_" + ("same-prefix-" * 7)
        request = ChatRequest(
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": common_prefix + "a",
                            "type": "function",
                            "function": {"name": "first", "arguments": "{}"},
                        },
                        {
                            "id": common_prefix + "b",
                            "type": "function",
                            "function": {"name": "second", "arguments": "{}"},
                        },
                    ],
                }
            ]
        )

        with patch(
            "core.llm_client.acompletion", new=AsyncMock(return_value=object())
        ) as mock_completion:
            await client.complete(provider, request)

        tool_calls = mock_completion.await_args.kwargs["messages"][0]["tool_calls"]
        normalized_ids = [tool_call["id"] for tool_call in tool_calls]
        self.assertEqual(len(set(normalized_ids)), 2)
        self.assertTrue(all(len(tool_call_id) == 64 for tool_call_id in normalized_ids))

    async def test_complete_omits_empty_optional_fields(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="gemini/gemini-2.0-flash",
            model="gemini-2.0-flash",
            base_url=None,
            api_key="gemini-secret",
            custom_llm_provider="gemini",
            is_codex=False,
        )
        request = ChatRequest(messages=[{"role": "user", "content": "hello"}])
        response = object()

        with patch("core.llm_client.acompletion", new=AsyncMock(return_value=response)) as mock_completion:
            result = await client.complete(provider, request)

        self.assertIs(result, response)
        kwargs = mock_completion.await_args.kwargs
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)
        self.assertNotIn("max_tokens", kwargs)
        self.assertNotIn("stream_options", kwargs)

    async def test_complete_resolves_local_image_paths(self):
        import base64
        from pathlib import Path

        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="openai/gpt-4o",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="openai-secret",
            custom_llm_provider=None,
            is_codex=False,
        )

        # Create a temp file in Path.cwd() / "temp" / "web_uploads" / "test_session"
        temp_dir = Path.cwd() / "temp" / "web_uploads" / "test_session"
        temp_dir.mkdir(parents=True, exist_ok=True)
        img_file = temp_dir / "test_image.png"
        img_content = b"fake image bytes"
        img_file.write_bytes(img_content)

        relative_url = f"/temp/web_uploads/test_session/test_image.png"

        request = ChatRequest(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "here is an image:"},
                        {"type": "image_url", "image_url": {"url": relative_url}},
                    ],
                }
            ]
        )

        response = object()
        try:
            with patch("core.llm_client.acompletion", new=AsyncMock(return_value=response)) as mock_completion:
                result = await client.complete(provider, request)

            self.assertIs(result, response)
            kwargs = mock_completion.await_args.kwargs
            processed_messages = kwargs["messages"]
            self.assertEqual(len(processed_messages), 1)
            content = processed_messages[0]["content"]
            self.assertEqual(content[0]["type"], "text")
            self.assertEqual(content[1]["type"], "image_url")
            
            expected_base64 = base64.b64encode(img_content).decode("utf-8")
            self.assertEqual(
                content[1]["image_url"]["url"],
                f"data:image/png;base64,{expected_base64}"
            )
        finally:
            if img_file.exists():
                img_file.unlink()


    async def test_deepseek_replays_reasoning_content_for_tool_turns(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="deepseek/deepseek-v4-flash",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="deepseek-secret",
            custom_llm_provider="openai",
            is_codex=False,
        )
        request = ChatRequest(
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "I should inspect the project.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "list_dir",
                                "arguments": '{"path":"."}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "main.py",
                },
                {"role": "user", "content": "continue"},
            ]
        )

        with patch(
            "core.llm_client.acompletion", new=AsyncMock(return_value=object())
        ) as mock_completion:
            await client.complete(provider, request)

        sent_messages = mock_completion.await_args.kwargs["messages"]
        self.assertEqual(
            sent_messages[0]["reasoning_content"],
            "I should inspect the project.",
        )

    async def test_deepseek_omits_tool_choice_in_thinking_mode(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="deepseek/deepseek-v4-flash",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="deepseek-secret",
            custom_llm_provider="openai",
            is_codex=False,
        )
        request = ChatRequest(
            messages=[{"role": "user", "content": "inspect the project"}],
            tools=[{"type": "function", "function": {"name": "list_dir"}}],
            tool_choice="required",
        )

        with patch(
            "core.llm_client.acompletion", new=AsyncMock(return_value=object())
        ) as mock_completion:
            await client.complete(provider, request)

        self.assertNotIn("tool_choice", mock_completion.await_args.kwargs)

    async def test_thinking_tool_choice_error_retries_without_tool_choice(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="openrouter/deepseek/deepseek-v4-flash",
            model="deepseek/deepseek-v4-flash",
            base_url="https://openrouter.ai/api/v1",
            api_key="openrouter-secret",
            custom_llm_provider="openai",
            is_codex=False,
        )
        request = ChatRequest(
            messages=[{"role": "user", "content": "inspect the project"}],
            tools=[{"type": "function", "function": {"name": "list_dir"}}],
            tool_choice="required",
        )
        error = Exception("Thinking mode does not support this tool_choice")

        with patch(
            "core.llm_client.acompletion",
            new=AsyncMock(side_effect=[error, object()]),
        ) as mock_completion:
            await client.complete(provider, request)

        self.assertEqual(mock_completion.await_count, 2)
        self.assertEqual(mock_completion.await_args_list[0].kwargs["tool_choice"], "required")
        self.assertNotIn("tool_choice", mock_completion.await_args_list[1].kwargs)

    async def test_deepseek_drops_legacy_tool_turns_without_reasoning(self):
        client = LimeLLMClient()
        provider = ProviderConfig(
            source_model="deepseek/deepseek-v4-flash",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="deepseek-secret",
            custom_llm_provider="openai",
            is_codex=False,
        )
        request = ChatRequest(
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_legacy",
                            "type": "function",
                            "function": {"name": "list_dir", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_legacy",
                    "content": "old result",
                },
                {"role": "user", "content": "run again"},
            ]
        )

        with patch(
            "core.llm_client.acompletion", new=AsyncMock(return_value=object())
        ) as mock_completion:
            await client.complete(provider, request)

        self.assertEqual(
            mock_completion.await_args.kwargs["messages"],
            [{"role": "user", "content": "run again"}],
        )


if __name__ == "__main__":
    unittest.main()
