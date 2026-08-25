import logging
import os
from typing import List, Dict, Any, Optional, Tuple

try:
    import httpx
except Exception:
    httpx = None

from core.oauth_profiles import resolve_codex_oauth_api_key

logger = logging.getLogger(__name__)
QWEN_COMPAT_BASE_URLS = [
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
]
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CURATED_MODEL_IDS = [
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "google/gemini-3.7-flash",
    "google/gemini-3.6-flash",
    "google/gemini-3.1-pro-preview",
    "inception/mercury-2",
    "meta-llama/llama-3.3-70b-instruct",
    "minimax/minimax-m2.5",
    "mistralai/codestral-2508",
    "mistralai/mistral-7b-instruct-v0.1",
    "mistralai/mistral-large",
    "mistralai/mistral-medium-3.1",
    "mistralai/mistral-small-3.2-24b-instruct-2506",
    "moonshotai/kimi-k3",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-sol-pro",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
    "openai/gpt-oss-120b",
    "perplexity/sonar",
    "perplexity/sonar-pro",
    "qwen/qwen3.8-27b",
    "x-ai/grok-4.6",
    "z-ai/glm-5.3",
]
OPENROUTER_CURATED_MODEL_ID_SET = frozenset(OPENROUTER_CURATED_MODEL_IDS)
_DIRECT_PROVIDER_PREFIXES = (
    "openai/",
    "openai-codex/",
    "anthropic/",
    "deepseek/",
    "moonshot/",
    "moonshotai/",
    "gemini/",
    "xai/",
    "qwen/",
    "nvidia/",
)


def _is_unprefixed_openrouter_model(model: str) -> bool:
    """Recognize legacy OpenRouter IDs without overriding explicit providers."""
    return model in OPENROUTER_CURATED_MODEL_ID_SET and not model.startswith(
        _DIRECT_PROVIDER_PREFIXES
    )

# Compatibility aliases for provider model IDs that were renamed or removed.
MODEL_ALIASES = {
    # DeepSeek retired the legacy aliases after the V4 rollout. Keep existing
    # LimeBot configurations working while exposing the current explicit IDs.
    "deepseek/deepseek-chat": "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-reasoner": "deepseek/deepseek-v4-flash",
    "xai/grok-4.1-fast-reasoning": "xai/grok-4.6",
    "xai/grok-4.1-fast-non-reasoning": "xai/grok-4.6",
    "grok-4.1-fast-reasoning": "xai/grok-4.6",
    "grok-4.1-fast-non-reasoning": "xai/grok-4.6",
    "nvidia/moonshotai/kimi-k2-5": "nvidia/moonshotai/kimi-k2.5",
    "nvidia/gpt-oss/120b": "nvidia/openai/gpt-oss-120b",
    "nvidia/gpt-oss/20b": "nvidia/openai/gpt-oss-20b",
    "nvidia/glm/4.7": "nvidia/z-ai/glm4.7",
    "nvidia/llama/4-scout": "nvidia/meta/llama-4-scout-17b-16e-instruct",
    "nvidia/llama/4-maverick": "nvidia/meta/llama-4-maverick-17b-128e-instruct",
    "moonshot/kimi-k2-5": "moonshot/kimi-k2.5",
    "moonshotai/kimi-k2-5": "moonshot/kimi-k2.5",
    "moonshotai/kimi-k2.5": "moonshot/kimi-k2.5",
}


async def fetch_openai_compatible_models(
    api_key: str, base_url: str, provider_name: str, prefix_id: bool = True
) -> List[Dict[str, Any]]:
    """
    Fetch models from an OpenAI-compatible API endpoint.

    Args:
        api_key: The API key for authentication.
        base_url: The base URL for the API (e.g., "https://api.openai.com/v1").
        provider_name: The name of the provider (e.g., "openai", "nvidia", "xai").
        prefix_id: Whether to prefix the model ID with the provider name (e.g., "nvidia/model-name").

    Returns:
        A list of model dictionaries suitable for the UI.
    """
    if not api_key:
        return []


    if httpx is None:
        logger.warning(
            "httpx is not installed; skipping OpenAI-compatible model fetch for %s",
            provider_name,
        )
        return []

    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)

            if response.status_code == 200:
                data = response.json()
                models = []
                for item in data.get("data", []):
                    model_id = item.get("id")
                    if not model_id:
                        continue

                    non_text_keywords = [
                        "embed",
                        "audio",
                        "dall-e",
                        "tts",
                        "stt",
                        "whisper",
                        "moderation",
                        "stable-diffusion",
                        "flux",
                        "rerank",
                        "bge-",
                        "gte-",
                        "clip",
                        "siglip",
                    ]
                    if any(kw in model_id.lower() for kw in non_text_keywords):
                        continue

                    display_name = model_id.split("/")[-1].replace("-", " ").title()
                    final_id = f"{provider_name}/{model_id}" if prefix_id else model_id

                    models.append(
                        {
                            "id": final_id,
                            "name": display_name,
                            "provider": provider_name,
                        }
                    )

                logger.info(f"Fetched {len(models)} models from {provider_name}")
                return models
            else:
                logger.warning(
                    f"Failed to fetch {provider_name} models: {response.status_code} - {response.text}"
                )
                return []
    except Exception as e:
        logger.error(f"Error fetching {provider_name} models: {e}")
        return []


async def fetch_gemini_models(api_key: str) -> List[Dict[str, Any]]:
    """Fetch current Gemini text-generation models from Google's model API."""
    if not api_key:
        return []
    if httpx is None:
        logger.warning("httpx is not installed; skipping Gemini model fetch")
        return []

    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={"key": api_key, "pageSize": 1000},
                timeout=10.0,
            )
            if response.status_code != 200:
                logger.warning(
                    "Failed to fetch Gemini models: %s - %s",
                    response.status_code,
                    response.text,
                )
                return []

            models = []
            for item in response.json().get("models", []):
                name = str(item.get("name") or "")
                model_id = name.removeprefix("models/")
                methods = item.get("supportedGenerationMethods") or []
                if not model_id or "generateContent" not in methods:
                    continue
                lower_id = model_id.lower()
                if any(
                    keyword in lower_id
                    for keyword in ("embedding", "image", "tts", "audio", "robotics")
                ):
                    continue
                display_name = item.get("displayName") or model_id
                models.append(
                    {
                        "id": f"gemini/{model_id}",
                        "name": str(display_name),
                        "provider": "gemini",
                    }
                )
            logger.info("Fetched %s models from Gemini", len(models))
            return models
    except Exception as exc:
        logger.error("Error fetching Gemini models: %s", exc)
        return []


async def fetch_anthropic_models(api_key: str) -> List[Dict[str, Any]]:
    """
    Fetch models from Anthropic API.
    Note: Anthropic's 'models' endpoint might behave differently or require specific headers.
    As of early 2026, standard listing might be limited, but we'll try the standard endpoint.
    """
    if not api_key:
        return []
    if httpx is None:
        logger.warning("httpx is not installed; skipping Anthropic model fetch")
        return []

    url = "https://api.anthropic.com/v1/models"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)

            if response.status_code == 200:
                data = response.json()
                models = []
                for item in data.get("data", []):
                    model_id = item.get("id")
                    display_name = item.get("display_name", model_id)

                    models.append(
                        {
                            "id": f"anthropic/{model_id}",
                            "name": display_name,
                            "provider": "anthropic",
                        }
                    )
                return models
            else:
                logger.warning(
                    f"Anthropic API list failed ({response.status_code}), using static list."
                )
                return []
    except Exception as e:
        logger.error(f"Error fetching Anthropic models: {e}")
        return []


def get_api_key_for_model(model: str) -> Optional[str]:
    """
    Resolve the correct API key from environment variables based on the model name.
    """
    if not model:
        return None
    if _is_unprefixed_openrouter_model(model):
        model = f"openrouter/{model}"

    if model.startswith("gemini/") or model.startswith("google/"):
        return os.getenv("GEMINI_API_KEY")
    elif model.startswith("openai-codex/"):
        # Codex OAuth is managed locally via the CLI helper, not .env secrets.
        try:
            return resolve_codex_oauth_api_key()
        except Exception as exc:
            logger.warning(f"Codex OAuth key unavailable: {exc}")
            return None
    elif model.startswith("openrouter/"):
        return os.getenv("OPENROUTER_API_KEY")
    elif model.startswith("openai/"):
        return os.getenv("OPENAI_API_KEY")
    elif model.startswith("anthropic/"):
        return os.getenv("ANTHROPIC_API_KEY")
    elif model.startswith("xai/"):
        return os.getenv("XAI_API_KEY")
    elif model.startswith("deepseek/"):
        return os.getenv("DEEPSEEK_API_KEY")
    elif model.startswith("moonshot/") or model.startswith("moonshotai/"):
        return (
            os.getenv("MOONSHOT_API_KEY")
            or os.getenv("MOONSHOTAI_API_KEY")
            or os.getenv("KIMI_API_KEY")
        )
    elif model.startswith("qwen/") or model.startswith("qwen-"):
        return os.getenv("DASHSCOPE_API_KEY")
    elif model.startswith("nvidia/"):
        return os.getenv("NVIDIA_API_KEY")

    # Fallback to any available key in a specific order
    return (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("XAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("MOONSHOT_API_KEY")
        or os.getenv("MOONSHOTAI_API_KEY")
        or os.getenv("KIMI_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("NVIDIA_API_KEY")
    )


def resolve_provider_config(model: str, default_base_url: Optional[str] = None) -> dict:
    """
    Resolve model, base_url, api_key, and custom_llm_provider for LiteLLM.
    """

    from config import load_config
    cfg = load_config()

    normalized_model = (model or "").strip()
    if normalized_model and "/" not in normalized_model and normalized_model.startswith("qwen-"):
        normalized_model = f"qwen/{normalized_model}"
    if normalized_model.startswith("moonshotai/"):
        normalized_model = f"moonshot/{normalized_model.removeprefix('moonshotai/')}"
    normalized_model = MODEL_ALIASES.get(normalized_model, normalized_model)
    if _is_unprefixed_openrouter_model(normalized_model):
        normalized_model = f"openrouter/{normalized_model}"

    api_key = get_api_key_for_model(normalized_model)
    base_url = default_base_url
    custom_llm_provider = None
    target_model = normalized_model
    proxy_url = getattr(cfg.llm, "proxy_url", "") if hasattr(cfg, "llm") else ""

    if proxy_url:
        base_url = proxy_url
    
    if normalized_model.startswith("nvidia/"):
        base_url = "https://integrate.api.nvidia.com/v1"
        target_model = normalized_model.removeprefix("nvidia/")
        custom_llm_provider = "nvidia_nim"
    elif normalized_model.startswith("openrouter/"):
        if not proxy_url:
            base_url = os.getenv("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL
        target_model = normalized_model.removeprefix("openrouter/")
        custom_llm_provider = "openai"
    elif normalized_model.startswith("xai/"):
        base_url = "https://api.x.ai/v1"
        target_model = normalized_model.removeprefix("xai/")
        custom_llm_provider = "openai"
    elif normalized_model.startswith("qwen/"):
        if not base_url:
            base_url = os.getenv("DASHSCOPE_BASE_URL") or QWEN_COMPAT_BASE_URLS[0]
        target_model = normalized_model.removeprefix("qwen/")
        custom_llm_provider = "openai"
    elif normalized_model.startswith("moonshot/"):
        if not base_url:
            base_url = (
                os.getenv("MOONSHOT_BASE_URL")
                or os.getenv("MOONSHOTAI_BASE_URL")
                or "https://api.moonshot.ai/v1"
            )
        target_model = normalized_model.removeprefix("moonshot/")
        custom_llm_provider = "openai"
    elif normalized_model.startswith("openai-codex/"):
        if not base_url:
            base_url = "https://chatgpt.com/backend-api/codex"
        target_model = normalized_model.removeprefix("openai-codex/")
        custom_llm_provider = "openai"
    elif normalized_model.startswith("gemini/"):
        target_model = normalized_model.removeprefix("gemini/")
        custom_llm_provider = "gemini"
    elif normalized_model.startswith("openai/"):
        target_model = normalized_model.removeprefix("openai/")
        # Pin direct OpenAI models explicitly. LiteLLM's registry may know a new
        # model through OpenRouter before its direct OpenAI mapping is updated;
        # leaving provider inference implicit can then substitute the
        # OPENROUTER_API_KEY and route the request to openrouter.ai.
        custom_llm_provider = "openai"
    elif normalized_model.startswith("anthropic/"):
        target_model = normalized_model.removeprefix("anthropic/")
    elif normalized_model.startswith("deepseek/"):
        if not base_url:
            base_url = "https://api.deepseek.com"
        target_model = normalized_model.removeprefix("deepseek/")
        custom_llm_provider = "openai"

    return {
        "model": target_model,
        "base_url": base_url,
        "api_key": api_key,
        "custom_llm_provider": custom_llm_provider,
    }


# The pi-ai registry still contains older Codex ids, but ChatGPT OAuth-backed
# Codex requests reject those legacy models. Keep automatic failover limited to
# the small set we know the ChatGPT Codex endpoint accepts.
CODEX_CHATGPT_FALLBACKS_BY_MODEL = {
    "openai-codex/gpt-5.6-sol": [
        "openai-codex/gpt-5.5",
        "openai-codex/gpt-5.4",
        "openai-codex/gpt-5.4-mini",
    ],
    "openai-codex/gpt-5.6-luna": [
        "openai-codex/gpt-5.4-mini",
        "openai-codex/gpt-5.4",
    ],
    "openai-codex/gpt-5.6-terra": [
        "openai-codex/gpt-5.5",
        "openai-codex/gpt-5.4",
        "openai-codex/gpt-5.4-mini",
    ],
    "openai-codex/gpt-5.5": ["openai-codex/gpt-5.4", "openai-codex/gpt-5.4-mini"],
    "openai-codex/gpt-5.4": ["openai-codex/gpt-5.4-mini"],
    "openai-codex/gpt-5.4-mini": ["openai-codex/gpt-5.4"],
}


def build_provider_chain(
    primary_model: str,
    fallback_models: List[str],
    default_base_url: Optional[str] = None,
) -> List[Tuple[str, dict]]:
    """
    Resolve the primary model plus fallbacks into an ordered provider chain.

    Returns a list of (source_model_name, provider_config_dict).
    """
    effective_fallbacks = list(fallback_models or [])

    normalized_primary = str(primary_model or "").strip()
    if normalized_primary in CODEX_CHATGPT_FALLBACKS_BY_MODEL:
        existing = {normalized_primary, *effective_fallbacks}
        for fallback_model in CODEX_CHATGPT_FALLBACKS_BY_MODEL[normalized_primary]:
            if fallback_model not in existing:
                effective_fallbacks.append(fallback_model)

    chain: List[Tuple[str, dict]] = []
    seen: set[str] = set()

    for raw_model in [primary_model, *effective_fallbacks]:
        model = str(raw_model or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        chain.append(
            (model, resolve_provider_config(model, default_base_url=default_base_url))
        )

    return chain
