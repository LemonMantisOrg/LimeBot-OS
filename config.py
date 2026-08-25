"""Configuration loader."""

import os
from types import SimpleNamespace
from dotenv import load_dotenv
from loguru import logger

from pathlib import Path
from core.runtime_paths import PROJECT_DIR, get_state_dir

_project_env_path = PROJECT_DIR / ".env"
if _project_env_path.exists():
    # Load the project file first so it can opt into LIMEBOT_STATE_DIR for
    # the rest of the configuration bootstrap.
    load_dotenv(dotenv_path=_project_env_path, override=False)

_state_env_path = get_state_dir() / ".env"
if _state_env_path.exists() and _state_env_path != _project_env_path:
    # Keep runtime/container env vars authoritative; only fill missing keys from .env.
    load_dotenv(dotenv_path=_state_env_path, override=False)
if not _state_env_path.exists() and not _project_env_path.exists():
    load_dotenv()


_cached_config = None
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}
_APPROVAL_POLICY_PROFILES = {"manual", "session", "autonomous", "review"}


def _load_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = str(raw_value).strip().lower()
    if not normalized:
        return default
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False

    logger.warning(f"Invalid {name} in .env, defaulting to {default}.")
    return default


def _load_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not str(raw_value).strip():
        return default

    try:
        return float(raw_value)
    except ValueError:
        logger.warning(f"Invalid {name} in .env, defaulting to {default}.")
        return default


def load_config(force_reload=False):
    """Load configuration from environment variables. Cached after first call."""
    global _cached_config
    if _cached_config is not None and not force_reload:
        return _cached_config

    config = SimpleNamespace()
    state_dir = get_state_dir()
    config.runtime = SimpleNamespace(state_dir=str(state_dir))

    config.discord = SimpleNamespace()
    config.discord.enabled = os.getenv("ENABLE_DISCORD", "true").lower() == "true"
    config.discord.token = os.getenv("DISCORD_TOKEN")
    config.discord.allow_from = [
        x for x in os.getenv("DISCORD_ALLOW_FROM", "").split(",") if x
    ]
    config.discord.allow_channels = [
        x for x in os.getenv("DISCORD_ALLOW_CHANNELS", "").split(",") if x
    ]
    config.discord.activity_type = os.getenv("DISCORD_ACTIVITY_TYPE", "playing")
    config.discord.activity_text = os.getenv("DISCORD_ACTIVITY_TEXT", "LimeBot")
    config.discord.status = os.getenv("DISCORD_STATUS", "online")
 
    config.web = SimpleNamespace()
    try:
        config.web.port = int(os.getenv("WEB_PORT") or os.getenv("PORT") or "8000")
    except ValueError:
        logger.warning("Invalid WEB_PORT/PORT in .env, defaulting to 8000.")
        config.web.port = 8000
    # Bind to loopback by default. Exposing the server on the LAN (0.0.0.0)
    # must be an explicit opt-in and, per the startup guard in channels/web.py,
    # requires APP_API_KEY so an unauthenticated agent is never reachable off-box.
    config.web.host = (os.getenv("WEB_HOST", "127.0.0.1") or "127.0.0.1").strip()
    frontend_port = (
        os.getenv("VITE_DEV_SERVER_PORT")
        or os.getenv("FRONTEND_PORT")
        or "5173"
    ).strip()
    default_web_origins = [
        f"http://localhost:{frontend_port}",
        f"http://127.0.0.1:{frontend_port}",
    ]
    raw_web_origins = os.getenv("WEB_ALLOWED_ORIGINS", "").strip()
    if raw_web_origins:
        parsed_web_origins = [
            origin.strip().rstrip("/")
            for origin in raw_web_origins.replace(";", ",").split(",")
            if origin.strip()
        ]
        config.web.allowed_origins = (
            default_web_origins if "*" in parsed_web_origins else parsed_web_origins
        )
    else:
        config.web.allowed_origins = default_web_origins

    config.whatsapp = SimpleNamespace()
    config.whatsapp.enabled = os.getenv("ENABLE_WHATSAPP", "false").lower() == "true"
    config.whatsapp.bridge_url = os.getenv("WHATSAPP_BRIDGE_URL", "ws://localhost:3000")
    config.whatsapp.allow_from = [
        x for x in os.getenv("WHATSAPP_ALLOW_FROM", "").split(",") if x
    ]

    config.telegram = SimpleNamespace()
    config.telegram.enabled = os.getenv("ENABLE_TELEGRAM", "false").lower() == "true"
    config.telegram.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    config.telegram.api_base = os.getenv(
        "TELEGRAM_API_BASE", "https://api.telegram.org"
    ).strip()
    config.telegram.allow_from = [
        x.strip() for x in os.getenv("TELEGRAM_ALLOW_FROM", "").split(",") if x.strip()
    ]
    config.telegram.allow_chats = [
        x.strip() for x in os.getenv("TELEGRAM_ALLOW_CHATS", "").split(",") if x.strip()
    ]
    try:
        config.telegram.poll_timeout = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
    except ValueError:
        logger.warning("Invalid TELEGRAM_POLL_TIMEOUT in .env, defaulting to 30.")
        config.telegram.poll_timeout = 30

    config.browser = SimpleNamespace()
    config.browser.mode = os.getenv("BROWSER_MODE", "isolated").strip().lower()
    config.browser.channel = os.getenv("BROWSER_CHANNEL", "").strip()
    config.browser.cdp_url = os.getenv("BROWSER_CDP_URL", "").strip()
    config.browser.user_data_dir = os.getenv("BROWSER_USER_DATA_DIR", "").strip()
    config.browser.profile_directory = os.getenv(
        "BROWSER_PROFILE_DIRECTORY", ""
    ).strip()

    config.video = SimpleNamespace()
    config.video.whisper_enabled = _load_bool_env(
        "VIDEO_WHISPER_ENABLED", default=False
    )

    # Hybrid web search: use a keyed provider when configured, otherwise fall
    # back to the keyless DuckDuckGo/browser chain. Keys are used in-process
    # only; _sanitized_env() strips them from spawned subprocesses.
    config.search = SimpleNamespace()
    config.search.provider = (
        os.getenv("SEARCH_PROVIDER", "auto").strip().lower() or "auto"
    )
    config.search.tavily_api_key = os.getenv("TAVILY_API_KEY", "").strip()
    config.search.brave_api_key = (
        os.getenv("BRAVE_SEARCH_API_KEY") or os.getenv("BRAVE_API_KEY") or ""
    ).strip()
    config.search.serpapi_api_key = (
        os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY") or ""
    ).strip()

    legacy_autonomous_mode = _load_bool_env("AUTONOMOUS_MODE", default=False)
    raw_approval_profile = str(os.getenv("APPROVAL_POLICY_PROFILE") or "").strip().lower()
    if raw_approval_profile and raw_approval_profile not in _APPROVAL_POLICY_PROFILES:
        logger.warning(
            "Invalid APPROVAL_POLICY_PROFILE in .env, defaulting to manual."
        )
        raw_approval_profile = "manual"
    config.approval_policy_profile = (
        raw_approval_profile
        or ("autonomous" if legacy_autonomous_mode else "manual")
    )
    # Keep old integrations working while the named profile remains authoritative.
    config.autonomous_mode = config.approval_policy_profile == "autonomous"
    config.unattended = SimpleNamespace()
    config.unattended.path_allowlist = [
        part.strip()
        for part in os.getenv("UNATTENDED_PATH_ALLOWLIST", "").split(",")
        if part.strip()
    ]
    config.unattended.command_allowlist = [
        part.strip()
        for part in os.getenv("UNATTENDED_COMMAND_ALLOWLIST", "").split(",")
        if part.strip()
    ]
    config.allow_unsafe_commands = (
        os.getenv("ALLOW_UNSAFE_COMMANDS", "false").lower() == "true"
    )
    try:
        config.max_iterations = int(os.getenv("MAX_ITERATIONS", "30"))
    except ValueError:
        logger.warning("Invalid MAX_ITERATIONS in .env, defaulting to 30.")
        config.max_iterations = 30

    new_recovery_budget = os.getenv("TOOL_RECOVERY_MAX_CORRECTIVE_FAILURES")
    legacy_recovery_budget = os.getenv("TOOL_RECOVERY_MAX_ATTEMPTS")
    legacy_budget_selected = new_recovery_budget is None and legacy_recovery_budget is not None
    try:
        recovery_budget = int(
            new_recovery_budget
            if new_recovery_budget is not None
            else legacy_recovery_budget
            if legacy_recovery_budget is not None
            else "5"
        )
    except ValueError:
        logger.warning(
            "Invalid tool recovery budget, defaulting to 5 corrective failures."
        )
        recovery_budget = 3 if legacy_budget_selected else 5
    if recovery_budget < 1:
        recovery_budget = 3 if legacy_budget_selected else 5
    # Keep the old setting as a compatibility alias while making the new
    # corrective-failure name the source of truth.
    config.tool_recovery_max_corrective_failures = recovery_budget
    config.tool_recovery_max_attempts = recovery_budget

    try:
        config.command_timeout = float(os.getenv("COMMAND_TIMEOUT", "0"))
    except ValueError:
        logger.warning("Invalid COMMAND_TIMEOUT in .env, defaulting to 0.")
        config.command_timeout = 0

    try:
        config.run_command_max_seconds = float(
            os.getenv("RUN_COMMAND_MAX_SECONDS", "180")
        )
    except ValueError:
        logger.warning(
            "Invalid RUN_COMMAND_MAX_SECONDS in .env, defaulting to 180."
        )
        config.run_command_max_seconds = 180.0
 
    try:
        config.tool_timeout = float(
            os.getenv("TOOL_TIMEOUT", os.getenv("COMMAND_TIMEOUT", "120.0"))
        )
    except ValueError:
        logger.warning("Invalid TOOL_TIMEOUT in .env, defaulting to 120.0.")
        config.tool_timeout = 120.0

    try:
        config.stall_timeout = float(os.getenv("STALL_TIMEOUT", "0"))
    except ValueError:
        logger.warning("Invalid STALL_TIMEOUT in .env, defaulting to 0.")
        config.stall_timeout = 0

    config.ai_harness = SimpleNamespace()
    ai_harness_mode = str(
        os.getenv("LIMEBOT_AI_HARNESS_MODE", "fast") or "fast"
    ).strip().lower()
    if not ai_harness_mode:
        ai_harness_mode = "fast"
    if ai_harness_mode not in {"balanced", "fast"}:
        logger.warning(
            "Invalid LIMEBOT_AI_HARNESS_MODE in .env, defaulting to fast."
        )
        ai_harness_mode = "fast"
    config.ai_harness.mode = ai_harness_mode
    config.ai_harness.fast_rag_timeout_s = _load_float_env(
        "LIMEBOT_FAST_RAG_TIMEOUT", 0.08
    )
    config.ai_harness.balanced_rag_timeout_s = _load_float_env(
        "LIMEBOT_BALANCED_RAG_TIMEOUT", 0.2
    )
    config.ai_harness.rag_timeout_s = (
        config.ai_harness.fast_rag_timeout_s
        if config.ai_harness.mode == "fast"
        else config.ai_harness.balanced_rag_timeout_s
    )
    config.ai_harness.fast_disable_tools_for_casual = _load_bool_env(
        "LIMEBOT_FAST_DISABLE_TOOLS_FOR_CASUAL", default=True
    )
    # Schema shortlisting is independent from the latency profile. LimeBot
    # exposes the full catalog unless an operator explicitly opts into the
    # bounded request-specific schema.
    config.tool_shortlist_enabled = _load_bool_env(
        "LIMEBOT_ENABLE_TOOL_SHORTLIST", default=False
    )

    from core.llm_utils import get_api_key_for_model

    config.llm = SimpleNamespace()
    default_llm_model = "gemini/gemini-3.6-flash"
    config.llm.model = str(os.getenv("LLM_MODEL") or "").strip() or default_llm_model
    config.llm.embedding_model = (
        str(os.getenv("LLM_EMBEDDING_MODEL") or "").strip() or None
    )
    config.llm.embedding_allow_local_fallback = _load_bool_env(
        "LLM_EMBEDDING_ALLOW_LOCAL_FALLBACK", default=False
    )
    raw_fallback_models = str(os.getenv("LLM_FALLBACK_MODELS") or "").strip()
    config.llm.fallback_models = [
        item.strip()
        for item in raw_fallback_models.replace(";", ",").split(",")
        if item.strip()
    ]
    config.llm.api_key = get_api_key_for_model(config.llm.model)
    config.llm.enable_dynamic_personality = (
        os.getenv("ENABLE_DYNAMIC_PERSONALITY", "false").lower() == "true"
    )
    config.llm.proxy_url = os.getenv("LLM_PROXY_URL", "")

    config.llm.base_url = os.getenv("LLM_BASE_URL")

    config.image_generation = SimpleNamespace()
    config.image_generation.model = (
        os.getenv("IMAGE_GENERATION_MODEL", "openai/gpt-image-2").strip()
        or "openai/gpt-image-2"
    )
    config.image_generation.size = (
        os.getenv("IMAGE_GENERATION_SIZE", "1024x1024").strip() or "1024x1024"
    )
    config.image_generation.quality = (
        os.getenv("IMAGE_GENERATION_QUALITY", "auto").strip() or "auto"
    )

    is_google_model = config.llm.model and (
        "gemini" in config.llm.model or "vertex" in config.llm.model
    )

    if config.llm.base_url and not is_google_model:
        clean_url = config.llm.base_url.rstrip("/")
        if clean_url.endswith("/api"):
            clean_url = clean_url[:-4]

        config.llm.base_url = clean_url
        os.environ["OPENAI_API_BASE"] = clean_url
    elif is_google_model and not config.llm.proxy_url:
        config.llm.base_url = None
        if "OPENAI_API_BASE" in os.environ:
            del os.environ["OPENAI_API_BASE"]

    is_local_llm = (
        config.llm.model
        and ("ollama" in config.llm.model or "local" in config.llm.model)
    ) or config.llm.base_url
    if not config.llm.api_key and not is_local_llm:
        logger.warning("No valid LLM API Key found in environment.")

    config.whitelist = SimpleNamespace()
    config.whitelist.allowed_paths = []

    # 1. Load from .env ALLOWED_PATHS (comma-separated)
    raw_paths = os.getenv("ALLOWED_PATHS", "")
    for p in raw_paths.replace(";", ",").split(","):
        if p.strip() and p.strip() not in config.whitelist.allowed_paths:
            config.whitelist.allowed_paths.append(p.strip())

    # 2. Load from allowed_paths.txt (one path per line), creating it if missing
    paths_file = state_dir / "allowed_paths.txt"
    if not os.path.exists(paths_file):
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with open(paths_file, "w", encoding="utf-8") as f:
                f.write("# Add your allowed workspace paths here, one per line.\n")
                f.write("# These directories will be accessible to LimeBot.\n")
                f.write("./persona\n")
                f.write("./logs\n")
        except Exception as e:
            logger.error(f"Error creating default allowed_paths.txt: {e}")

    if os.path.exists(paths_file):
        try:
            with open(paths_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if (
                        line
                        and not line.startswith("#")
                        and line not in config.whitelist.allowed_paths
                    ):
                        config.whitelist.allowed_paths.append(line)
        except Exception as e:
            logger.error(f"Error reading allowed_paths.txt: {e}")

    limebot_root = str(PROJECT_DIR)
    for allowed_root in (limebot_root, str(state_dir)):
        if allowed_root not in config.whitelist.allowed_paths:
            config.whitelist.allowed_paths.append(allowed_root)

    if not config.unattended.path_allowlist:
        config.unattended.path_allowlist = list(config.whitelist.allowed_paths)

    config.whitelist.api_key = os.getenv("APP_API_KEY")
    config.personality_whitelist = [
        x.strip()
        for x in os.getenv("PERSONALITY_WHITELIST", "").split(",")
        if x.strip()
    ]

    if config.llm.api_key and is_google_model:
        os.environ.setdefault("GEMINI_API_KEY", config.llm.api_key)
        os.environ.setdefault("GOOGLE_API_KEY", config.llm.api_key)

    limebot_config_path = state_dir / "limebot.json"
    if limebot_config_path.exists():
        import json

        try:
            with open(limebot_config_path, "r", encoding="utf-8") as f:
                dynamic_config = json.load(f)

                if "skills" in dynamic_config:
                    config.skills = SimpleNamespace(**dynamic_config["skills"])
                else:
                    config.skills = SimpleNamespace(disabled=[])

                if "allowed_paths" in dynamic_config and isinstance(
                    dynamic_config["allowed_paths"], list
                ):
                    for p in dynamic_config["allowed_paths"]:
                        if (
                            isinstance(p, str)
                            and p.strip()
                            and p.strip() not in config.whitelist.allowed_paths
                        ):
                            config.whitelist.allowed_paths.append(p.strip())

                if "llm" in dynamic_config and isinstance(dynamic_config["llm"], dict):
                    for k, v in dynamic_config["llm"].items():
                        if k == "model":
                            logger.warning(
                                "Ignoring deprecated llm.model in limebot.json; use LLM_MODEL in .env instead."
                            )
                            continue
                        setattr(config.llm, k, v)
                if "discord" in dynamic_config and isinstance(
                    dynamic_config["discord"], dict
                ):
                    for k, v in dynamic_config["discord"].items():
                        setattr(config.discord, k, v)
                if "telegram" in dynamic_config and isinstance(
                    dynamic_config["telegram"], dict
                ):
                    for k, v in dynamic_config["telegram"].items():
                        setattr(config.telegram, k, v)
                if "browser" in dynamic_config and isinstance(
                    dynamic_config["browser"], dict
                ):
                    for k, v in dynamic_config["browser"].items():
                        setattr(config.browser, k, v)
        except Exception as e:
            logger.error(f"Error loading limebot.json: {e}")
            config.skills = SimpleNamespace(disabled=[])
    else:
        config.skills = SimpleNamespace(disabled=[])

    config.llm.model = str(getattr(config.llm, "model", "") or "").strip()
    if not config.llm.model:
        logger.warning(
            "LLM model resolved empty after config load; falling back to default model."
        )
        config.llm.model = default_llm_model
    config.llm.api_key = get_api_key_for_model(config.llm.model)
    config.llm.fallback_models = [
        model
        for model in getattr(config.llm, "fallback_models", [])
        if isinstance(model, str) and model.strip() and model.strip() != config.llm.model
    ]

    _cached_config = config
    return config


def reload_config():
    """Force-reload config (call after .env changes)."""
    return load_config(force_reload=True)
