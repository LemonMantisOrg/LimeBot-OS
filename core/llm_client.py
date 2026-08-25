"""Small OpenAI-compatible chat client for LimeBot.

LimeBot's canonical internal chat format is OpenAI-compatible messages plus
OpenAI function tool schemas. LiteLLM handles provider-specific execution for
most providers. core.codex_bridge is the adapter for openai-codex/*.
"""

from __future__ import annotations

import asyncio
import base64
import copy
from dataclasses import dataclass
import hashlib
import logging
import mimetypes
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_OPENAI_TOOL_CALL_ID_MAX_LENGTH = 64


def _provider_requires_reasoning_none_for_tools(error: Exception) -> bool:
    """Recognize the OpenAI-compatible error for tool calls plus reasoning.

    Some newer OpenAI-compatible models expose reasoning on chat completions but
    reject function tools while reasoning is enabled. The provider tells us the
    safe compatibility setting in its error, so keep the retry narrowly scoped
    instead of disabling reasoning for every model.
    """
    message = str(error).lower()
    return (
        "function tools with reasoning_effort" in message
        and "reasoning_effort" in message
        and "none" in message
    )


def _provider_rejects_tool_choice_in_thinking_mode(error: Exception) -> bool:
    """Recognize providers that support tools but reject the tool_choice field."""
    message = str(error).lower()
    return (
        "thinking mode" in message
        and "does not support" in message
        and "tool_choice" in message
    )


def _provider_rejects_missing_reasoning_replay(error: Exception) -> bool:
    """Recognize a provider-confirmed missing DeepSeek reasoning trace."""
    message = str(error).lower()
    return (
        "reasoning_content" in message
        and "thinking mode" in message
        and (
            "must be passed back" in message
            or "must be provided" in message
            or "cannot be omitted" in message
        )
    )


def _is_deepseek_provider(
    source_model: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
) -> bool:
    """Recognize DeepSeek even when it is routed through a gateway.

    The source model is not always a direct ``deepseek/...`` ID. OpenRouter,
    custom proxies, and older saved settings can expose the same model as
    ``openrouter/deepseek/...`` or simply ``deepseek-vX``. History replay rules
    are provider rules, so inspect every non-secret provider identity field at
    the common request boundary.
    """
    return any(
        "deepseek" in str(value or "").strip().lower()
        for value in (source_model, model, base_url)
    )


def _is_direct_deepseek_thinking_endpoint(
    source_model: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
) -> bool:
    """Return whether the request targets DeepSeek's own thinking endpoint.

    Gateways may translate or accept ``tool_choice`` differently. Keep their
    compatibility retry behavior intact while still applying DeepSeek history
    replay rules to gateway-routed requests.
    """
    source = str(source_model or "").strip().lower()
    endpoint = str(base_url or "").strip().lower()
    model_name = str(model or "").strip().lower()
    return source.startswith("deepseek/") or (
        "deepseek" in model_name
        and "deepseek.com" in endpoint
        and "openrouter" not in endpoint
    )


def _bounded_tool_call_id(tool_call_id: Any) -> str:
    """Return a deterministic OpenAI-safe ID without breaking tool-result links."""
    normalized = str(tool_call_id or "")
    if len(normalized) <= _OPENAI_TOOL_CALL_ID_MAX_LENGTH:
        return normalized

    # A fixed safe prefix plus a digest avoids collisions between provider IDs that
    # share a long prefix. The complete result is exactly 64 characters.
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"call_{digest[:_OPENAI_TOOL_CALL_ID_MAX_LENGTH - 5]}"


def _normalize_openai_tool_call_ids(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Bound tool-call IDs and keep matching tool messages synchronized."""
    replacements: Dict[str, str] = {}

    for message in messages:
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict) or "id" not in tool_call:
                continue
            original_id = str(tool_call.get("id") or "")
            bounded_id = _bounded_tool_call_id(original_id)
            replacements[original_id] = bounded_id
            tool_call["id"] = bounded_id

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        original_id = str(message.get("tool_call_id") or "")
        if not original_id:
            continue
        message["tool_call_id"] = replacements.get(
            original_id, _bounded_tool_call_id(original_id)
        )

    return messages


def _drop_unreplayable_deepseek_tool_turns(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove legacy DeepSeek tool turns whose reasoning trace was not stored.

    DeepSeek requires the complete ``reasoning_content`` for assistant messages
    that contain tool calls. Older LimeBot history entries were written without
    that field, so replaying them produces a 400 before the new turn can run.
    The exact hidden reasoning cannot be reconstructed; dropping only that old
    assistant/tool exchange lets the current user turn continue safely.
    """
    missing_reasoning_call_ids: set[str] = set()
    repaired: List[Dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and isinstance(
            message.get("tool_calls"), list
        ):
            reasoning = str(message.get("reasoning_content") or "").strip()
            if not reasoning:
                for tool_call in message.get("tool_calls") or []:
                    if isinstance(tool_call, dict) and tool_call.get("id"):
                        missing_reasoning_call_ids.add(str(tool_call["id"]))
                continue
        if (
            message.get("role") == "tool"
            and str(message.get("tool_call_id") or "")
            in missing_reasoning_call_ids
        ):
            continue
        repaired.append(message)

    return repaired


def _resolve_local_image_urls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Scan messages for local image URLs (e.g. starting with '/temp/') and resolve them to base64 data URLs.
    
    This avoids LiteLLM/OpenAI throwing 'Invalid URL format' for relative/absolute local file paths.
    """
    copied_messages = copy.deepcopy(messages)
    for msg in copied_messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_url_obj = part.get("image_url")
                    if isinstance(image_url_obj, dict):
                        url = image_url_obj.get("url")
                        if isinstance(url, str) and (url.startswith("/temp/") or url.startswith("temp/")):
                            # Remove leading slash to make it relative to current working directory
                            local_path = Path.cwd() / url.lstrip('/')
                            if local_path.exists() and local_path.is_file():
                                try:
                                    mime_type, _ = mimetypes.guess_type(local_path)
                                    if not mime_type:
                                        mime_type = "image/png"
                                    data = local_path.read_bytes()
                                    encoded = base64.b64encode(data).decode("utf-8")
                                    image_url_obj["url"] = f"data:{mime_type};base64,{encoded}"
                                except Exception as e:
                                    logger.error(f"Error encoding local image {local_path}: {e}")
                            else:
                                logger.warning(f"Local image path {local_path} does not exist or is not a file.")
    return copied_messages

try:
    from litellm import acompletion
except Exception:
    acompletion = None

from core.codex_bridge import (
    complete_codex_response,
    is_codex_model_name,
    stream_codex_response,
)
from core.llm_utils import build_provider_chain, resolve_provider_config


@dataclass(frozen=True)
class ProviderConfig:
    source_model: str
    model: str
    base_url: Optional[str]
    api_key: Optional[str]
    custom_llm_provider: Optional[str]
    is_codex: bool = False


@dataclass(frozen=True)
class ChatRequest:
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]] = None
    stream: bool = False
    max_tokens: Optional[int] = None
    session_id: Optional[str] = None
    tool_choice: Optional[str] = "auto"


class LimeLLMClient:
    @staticmethod
    def _provider_from_mapping(
        source_model: str, provider_cfg: Dict[str, Any]
    ) -> ProviderConfig:
        return ProviderConfig(
            source_model=source_model,
            model=provider_cfg["model"],
            base_url=provider_cfg["base_url"],
            api_key=provider_cfg["api_key"],
            custom_llm_provider=provider_cfg["custom_llm_provider"],
            is_codex=is_codex_model_name(source_model),
        )

    def resolve_provider(
        self, model: str, default_base_url: Optional[str] = None
    ) -> ProviderConfig:
        provider_cfg = resolve_provider_config(model, default_base_url=default_base_url)
        return self._provider_from_mapping(model, provider_cfg)

    def resolve_chain(
        self,
        primary_model: str,
        fallback_models: List[str],
        default_base_url: Optional[str] = None,
    ) -> List[ProviderConfig]:
        chain = build_provider_chain(
            primary_model,
            fallback_models,
            default_base_url=default_base_url,
        )
        return [
            self._provider_from_mapping(source_model, provider_cfg)
            for source_model, provider_cfg in chain
        ]

    async def complete(self, provider: ProviderConfig, request: ChatRequest) -> Any:
        test_mode = str(os.getenv("LIMEBOT_TEST_LLM_MODE") or "").strip().lower()
        if test_mode:
            sleep_s = float(os.getenv("LIMEBOT_TEST_LLM_SLEEP") or "0" or 0)
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            reply = os.getenv("LIMEBOT_TEST_LLM_REPLY") or "Durable job completed."
            if request.stream:
                return _TestLLMStream(reply)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply, tool_calls=None))],
                usage=None,
            )
        messages = _resolve_local_image_urls(request.messages)
        if provider.is_codex:
            helper = stream_codex_response if request.stream else complete_codex_response
            return await asyncio.to_thread(
                helper,
                provider.source_model,
                messages,
                request.tools,
                request.session_id,
                request.tool_choice,
            )

        if _is_deepseek_provider(
            provider.source_model,
            provider.model,
            provider.base_url,
        ):
            repaired_messages = _drop_unreplayable_deepseek_tool_turns(messages)
            if len(repaired_messages) != len(messages):
                logger.warning(
                    "Removed legacy DeepSeek tool history without reasoning_content "
                    "before replaying %s.",
                    provider.source_model,
                )
                messages = repaired_messages

        messages = _normalize_openai_tool_call_ids(messages)

        if acompletion is None:
            raise RuntimeError("litellm is not installed")

        kwargs: Dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "stream": request.stream,
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "custom_llm_provider": provider.custom_llm_provider,
        }

        if request.tools:
            kwargs["tools"] = request.tools
            # DeepSeek V4 thinking mode supports tool calls but rejects the
            # OpenAI-compatible tool_choice parameter, including "auto".
            # The prompt and tool schemas still give the model the required
            # context; forcing a choice is not available on this endpoint.
            deepseek_thinking = _is_direct_deepseek_thinking_endpoint(
                provider.source_model,
                provider.model,
                provider.base_url,
            )
            if request.tool_choice is not None and not deepseek_thinking:
                kwargs["tool_choice"] = request.tool_choice
        if request.stream:
            kwargs["stream_options"] = {"include_usage": True}
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens

        try:
            return await acompletion(**kwargs)
        except Exception as exc:
            if _provider_rejects_missing_reasoning_replay(exc):
                repaired_messages = _drop_unreplayable_deepseek_tool_turns(messages)
                if len(repaired_messages) != len(messages):
                    compatibility_kwargs = dict(kwargs)
                    compatibility_kwargs["messages"] = repaired_messages
                    logger.warning(
                        "Provider rejected an unreplayable reasoning trace for %s; "
                        "retrying without the affected legacy tool exchange.",
                        provider.source_model,
                    )
                    return await acompletion(**compatibility_kwargs)
            if request.tools and _provider_rejects_tool_choice_in_thinking_mode(exc):
                compatibility_kwargs = dict(kwargs)
                compatibility_kwargs.pop("tool_choice", None)
                logger.warning(
                    "Provider rejected tool_choice in thinking mode for %s; "
                    "retrying without tool_choice.",
                    provider.source_model,
                )
                return await acompletion(**compatibility_kwargs)
            if not request.tools or not _provider_requires_reasoning_none_for_tools(exc):
                raise

            compatibility_kwargs = dict(kwargs)
            compatibility_kwargs["reasoning_effort"] = "none"
            logger.warning(
                "Provider rejected reasoning with function tools for %s; "
                "retrying with reasoning_effort=none.",
                provider.source_model,
            )
            return await acompletion(**compatibility_kwargs)


class _TestLLMStream:
    """Async iterator that mimics a LiteLLM streaming completion."""

    def __init__(self, content: str):
        self._chunks = [
            SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None)
                    )
                ],
            )
        ]
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk
