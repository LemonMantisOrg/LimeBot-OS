import asyncio
import ast
import base64
import contextvars
import hashlib
import json
import mimetypes
import os
import re
import shlex
import time
import uuid
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.confirmation import (
    ConfirmationManager,
    SENSITIVE_TOOLS,
    APPROVE_WORDS,
    DENY_WORDS,
)
from core.rag_engine import RagEngine, AUTORAG_MIN_SCORE
from core.tool_dispatcher import (
    normalize_tool_alias,
    TOOL_RESULT_LIMITS,
    DEFAULT_TOOL_RESULT_LIMIT,
    BROWSER_CACHEABLE,
    TAG_COMPAT_TOOLS,
    TOOL_NAME_ALIASES,
    truncate_tool_result,
)

try:
    from litellm import (
        RateLimitError,
        InternalServerError,
        APIConnectionError,
        ServiceUnavailableError,
        AuthenticationError,
    )
except Exception:
    class _LiteLLMFallbackError(Exception):
        def __init__(self, *args, **kwargs):
            message = kwargs.get("message")
            if message is None and args:
                message = args[0]
            super().__init__(message or "")

    class RateLimitError(_LiteLLMFallbackError):
        pass

    class InternalServerError(_LiteLLMFallbackError):
        pass

    class APIConnectionError(_LiteLLMFallbackError):
        pass

    class ServiceUnavailableError(_LiteLLMFallbackError):
        pass

    class AuthenticationError(_LiteLLMFallbackError):
        pass
from loguru import logger

from config import load_config
from core.browser import get_browser_manager
from core.bus import MessageBus
from core.cache import ToolCache
from core.context import tool_context
from core.events import InboundMessage, OutboundMessage
from core.llm_client import ChatRequest, LimeLLMClient, ProviderConfig
from core.managed_tasks import ManagedTaskRegistry
from core import prompt as prompt_module
from core.metrics import MetricsCollector
from core.prompt_modes import (
    build_ponytail_prompt_addition,
    normalize_ponytail_mode,
)
from core.redaction import redact_sensitive_text, redact_sensitive_value
from core.provider_circuit_breaker import (
    ProviderCircuitBreaker,
    ProviderCircuitOpenError,
)
from core.runtime_paths import get_skill_dirs
from core.skill_invocation import parse_skill_invocation
from core.session_manager import SessionManager
from core.skills import SkillRegistry
from core.subagents import SubagentRegistry, normalize_subagent_tool_name
from core.tag_parser import process_tags
from core.tool_defs import shortlist_tool_definitions
from core.tools import Toolbox
from core.vectors import get_vector_service


TOOL_BROADCAST_MAX_CHARS = 500
_SUBAGENT_REPORT_DIFF_MAX_CHARS = 10_000
_TOOL_LOCAL_IMAGE_MAX_BYTES = 4 * 1024 * 1024
_RECENT_IMAGE_REFERENCE_TTL_S = 30 * 60
_TOOL_MEDIA_PAYLOAD_START = "<limebot-tool-payload>"
_TOOL_MEDIA_PAYLOAD_END = "</limebot-tool-payload>"
_HISTORY_TOOL_OUTPUT_MAX_CHARS = 8_000
_HISTORY_INTERRUPTED_TOOL_MESSAGE = (
    "[Tool call interrupted before a result was recorded. The previous runtime "
    "did not persist a tool result.]"
)


# Tool result limits and browser cacheability are now in core/tool_dispatcher.py
# and imported at the top of this file as TOOL_RESULT_LIMITS, DEFAULT_TOOL_RESULT_LIMIT,
# BROWSER_CACHEABLE, TAG_COMPAT_TOOLS.
# AUTORAG_MIN_SCORE is imported from core/rag_engine.py.
# SENSITIVE_TOOLS, APPROVE_WORDS, DENY_WORDS come from core/confirmation.py.

_INTERIM_SAVE_EVERY = 5
_CURRENT_TASK_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "limebot_current_task_id", default=None
)

CODING_PHASES = frozenset(
    {"inspect", "plan", "apply", "verify", "repair", "complete", "blocked"}
)
_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "capability_search", "read_file", "list_dir", "search_files", "verify_files", "diagnose_files", "memory_search", "web_search",
        "image_search", "deep_research", "browser_extract", "browser_get_page_text",
        "browser_snapshot", "browser_list_media", "google_search",
    }
)
_MUTATION_TOOL_NAMES = frozenset(
    {"edit_file", "write_file", "create_spreadsheet", "delete_file"}
)
_RESEARCH_TOOL_NAMES = frozenset(
    {
        "web_search",
        "google_search",
        "deep_research",
        "image_search",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_snapshot",
        "browser_scroll",
        "browser_wait",
        "browser_press_key",
        "browser_go_back",
        "browser_tabs",
        "browser_switch_tab",
        "browser_extract",
        "browser_get_page_text",
        "browser_list_media",
        "browser_download",
    }
)
_ARTIFACT_REQUEST_RE = re.compile(
    r"\b(?:xlsx|excel|spreadsheet|csv|pdf|docx|report|document|download|export|"
    r"attachment|attach|file|archivo|adjunta|adjuntar|descarga|descargar|exporta|"
    r"exportar|informe|documento|hoja\s+de\s+c[aá]lculo)\b",
    re.IGNORECASE,
)
_CODING_HINT_RE = re.compile(
    r"\b(code|coding|program|repo|repository|bug|fix|test|pytest|lint|build|compile|"
    r"implement|refactor|function|file|patch|error|failure)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ToolOutcome:
    """Bounded, redaction-safe outcome retained for coding recovery."""

    tool: str
    success: bool
    exit_code: Optional[int]
    timed_out: bool
    stalled: bool
    retry_safe: bool
    failure_fingerprint: str
    diagnostic_head: str
    diagnostic_tail: str
    verification_status: Optional[str] = None
    verification_detail: str = ""

_CASUAL_WORDS = frozenset(
    {
        "hi",
        "hey",
        "hello",
        "yo",
        "sup",
        "ok",
        "okay",
        "k",
        "yes",
        "no",
        "nope",
        "yep",
        "sure",
        "thanks",
        "thank you",
        "lol",
        "lmao",
        "haha",
        "nice",
        "cool",
        "good",
        "great",
        "bye",
        "cya",
        "ttyl",
        "brb",
    }
)

_FAST_TOOL_ACTION_VERBS = frozenset(
    {
        "list",
        "read",
        "open",
        "search",
        "browse",
        "send",
        "schedule",
        "remind",
        "create",
        "write",
        "delete",
        "run",
        "install",
        "show",
        "find",
        "inspect",
        # Short Spanish follow-ups are action requests too.  Without these,
        # fast mode mistakes commands such as "hazlo" for casual smalltalk and
        # removes the tools needed to continue the previous task.
        "haz",
        "hazlo",
        "hazla",
        "hazme",
        "continua",
        "corrige",
        "arregla",
        "edita",
        "revisa",
    }
)

_CASUAL_PHRASE_PREFIXES = (
    "how are you",
    "how's it going",
    "hows it going",
    "what's up",
    "whats up",
    "good morning",
    "good night",
    "good evening",
    "thank you",
    "thanks",
)

_EXPLICIT_TOOL_REQUEST_RE = re.compile(
    r"\b(?:browse|open|visit|navigate|search|research|investigate|download|export|"
    r"abrir|abre|visitar|navegar|buscar|busca|investigar|investiga|investigue|"
    r"descargar|descarga|exportar|exporta|utilizar|utiliza|utilice|probar|prueba)\b",
    re.IGNORECASE,
)

# Some providers occasionally serialize a native tool call into assistant text
# instead of the structured tool-call field.  Recover the exact visible form
# emitted by those providers and keep it out of the user-facing response.
_VISIBLE_PROVIDER_TOOL_CALL_PREFIX_RE = re.compile(
    r"(?:^|\s)to\s*=\s*functions\.(?P<name>[A-Za-z_][\w]*)"
    r"(?:\s+code)?\s*:\s*",
    re.IGNORECASE,
)

_GHOST_TAG_RE = re.compile(
    r"</?(?:save_user|save_soul|save_identity|save_memory"
    r"|log_memory|save_mood|save_relationship"
    r"|save_memory|discord_send|discord_embed)>",
    re.IGNORECASE,
)

_GHOST_TAG_NAMES = (
    "save_user",
    "save_soul",
    "save_identity",
    "save_memory",
    "log_memory",
    "save_mood",
    "save_relationship",
    "discord_send",
    "discord_embed",
)

# Backward-compat aliases for code that still uses the underscore-prefixed names
_TOOL_RESULT_LIMITS = TOOL_RESULT_LIMITS
_DEFAULT_TOOL_RESULT_LIMIT = DEFAULT_TOOL_RESULT_LIMIT
_BROWSER_CACHEABLE = BROWSER_CACHEABLE
_TAG_COMPAT_TOOLS = TAG_COMPAT_TOOLS
_SENSITIVE_TOOLS = SENSITIVE_TOOLS
_APPROVE_WORDS = APPROVE_WORDS
_DENY_WORDS = DENY_WORDS
_AGENT_READINESS_TIMEOUT_S = 20.0
_LLM_WARMUP_MAX_TOKENS = 16

# Search tools route through core/web_search.py (provider layer) rather than the
# Playwright browser stack. google_search is kept as a back-compat alias.
_SEARCH_TOOLS = frozenset(
    {"web_search", "image_search", "deep_research", "google_search"}
)

from core.paths import PERSONA_DIR, USERS_DIR, MEMORY_DIR, SOUL_FILE, IDENTITY_FILE


class AgentLoop:
    """Agent loop supporting interactive persona setup and user context."""

    def __init__(
        self,
        bus: MessageBus,
        model: str = "gpt-3.5-turbo",
        scheduler: Any = None,
        session_manager: Optional[SessionManager] = None,
    ):
        self.bus = bus
        self.model = model
        self.scheduler = scheduler
        self._running = False

        for d in (PERSONA_DIR, USERS_DIR, MEMORY_DIR):
            d.mkdir(exist_ok=True)

        self.history: Dict[str, List[Dict]] = {}
        self.session_locks: Dict[str, asyncio.Lock] = {}
        self.session_whitelists: Dict[str, Set[str]] = {}

        self.session_manager = session_manager or SessionManager()
        self.metrics = MetricsCollector()
        self.llm_client = LimeLLMClient()
        self.tool_cache = ToolCache()
        self.pending_confirmations: Dict[str, Dict[str, Any]] = {}
        self.active_tasks: Dict[str, asyncio.Task] = {}
        # One registry owns every live turn and background job.  The legacy
        # maps below remain as compatibility indexes for channel adapters, but
        # lifecycle operations use this registry as the source of truth.
        self.task_registry = ManagedTaskRegistry()
        # Background subagents are durable TaskTracker records plus live
        # asyncio handles. The handle map is intentionally separate from
        # active_tasks because these jobs outlive the parent turn.
        self.background_subagent_tasks: Dict[str, asyncio.Task] = {}
        self.background_subagent_sessions: Dict[str, str] = {}
        self.background_subagent_parents: Dict[str, str] = {}
        self.background_subagent_results: Dict[str, str] = {}

        self._history_dirty: Dict[str, bool] = {}

        self._last_msg_hash: Dict[str, Tuple[int, float]] = {}

        cfg = load_config()
        self.config = cfg
        if cfg.llm.model:
            self.model = cfg.llm.model
        self.primary_model = self.model
        self.fallback_models = list(getattr(cfg.llm, "fallback_models", []) or [])
        self.provider_circuit_breaker = ProviderCircuitBreaker()

        self._provider: Tuple = self._resolve_provider()

        self.toolbox = Toolbox(
            allowed_paths=cfg.whitelist.allowed_paths, bus=bus, config=cfg
        )
        self.toolbox.set_agent(self)
        if self.scheduler:
            self.toolbox.set_scheduler(self.scheduler)

        self.vector_service = get_vector_service(cfg)

        self._tool_registry: Dict[str, Any] = {
            "read_file": self.toolbox.read_file,
            "edit_file": self.toolbox.edit_file,
            "write_file": self.toolbox.write_file,
            "create_spreadsheet": self.toolbox.create_spreadsheet,
            "calculate": self.toolbox.calculate,
            "delete_file": self.toolbox.delete_file,
            "list_dir": self.toolbox.list_dir,
            "search_files": self.toolbox.search_files,
            "verify_files": self.toolbox.verify_files,
            "diagnose_files": self.toolbox.diagnose_files,
            "run_command": self.toolbox.run_command,
            "memory_search": self.toolbox.memory_search,
            "memory_save": self.toolbox.memory_save,
            "send_media": self.toolbox.send_media,
            "send_voice": self.toolbox.send_voice,
            "generate_image": self.toolbox.generate_image,
            "analyze_video": self.toolbox.analyze_video,
            "send_discord_message": self.toolbox.send_discord_message,
            "send_discord_embed": self.toolbox.send_discord_embed,
            "list_discord_channels": self.toolbox.list_discord_channels,
            "cron_add": self.toolbox.cron_add,
            "cron_list": self.toolbox.cron_list,
            "cron_remove": self.toolbox.cron_remove,
            "create_skill": self.toolbox.create_skill,
        }

        self.skill_registry = SkillRegistry(skill_dirs=[str(path) for path in get_skill_dirs()], config=cfg)
        self.subagent_registry = SubagentRegistry()
        self.toolbox.set_subagent_registry(self.subagent_registry)

        # ── Sub-module managers ──────────────────────────────────────────
        self.confirm = ConfirmationManager(
            toolbox=self.toolbox,
            truncate_fn=self._truncate_preview,
            safe_json_load_fn=self._safe_json_load,
        )
        self.rag = RagEngine(
            truncate_fn=self._truncate_preview,
            safe_json_load_fn=self._safe_json_load,
        )

        self._tool_definitions: Optional[List[Dict]] = None
        self._warmed = False
        self._readiness_started_at = time.perf_counter()
        self._readiness_phase = "created"
        self._readiness_phase_history = ["created"]
        self._readiness_status = "starting"
        self._readiness_degraded_reasons: List[str] = []
        self._readiness_failure_code: Optional[str] = None
        self._readiness_event = asyncio.Event()
        self._initialization_task = asyncio.create_task(
            self._initialize_capabilities()
        )

        self._stable_prompt_cache: Dict[str, Tuple[str, float]] = {}
        self._STABLE_PROMPT_TTL = 30.0
        # Per-session task capability context.  The latest substantive request
        # remains available when the next utterance is only an acknowledgement
        # or shorthand follow-up ("yes, that one", "sí la tienes", etc.).
        self._session_capability_state: Dict[str, Dict[str, Any]] = {}
        self._capability_snapshot_revision = 0
        self._mcp_snapshot_signature: Tuple[Any, ...] = ()
        self._history_flush_interval = 5.0
        self._last_history_flush: Dict[str, float] = {}
        self._image_input_fallback_sessions: Set[str] = set()
        self._sessions_pending_tool_image_reply: Set[str] = set()
        self._recent_image_attachments: Dict[
            str, Tuple[float, List[Dict[str, Any]]]
        ] = {}
        self._workspace_changesets: Dict[str, Tuple[str, str]] = {}

    def _set_readiness_phase(self, phase: str) -> None:
        self._readiness_phase = phase
        if not self._readiness_phase_history or self._readiness_phase_history[-1] != phase:
            self._readiness_phase_history.append(phase)
        logger.debug(f"Agent readiness phase: {phase}")

    def _add_readiness_degradation(self, reason: str) -> None:
        if reason not in self._readiness_degraded_reasons:
            self._readiness_degraded_reasons.append(reason)

    def get_readiness_status(self) -> Dict[str, Any]:
        ready = self._readiness_status in {"ready", "degraded"}
        return {
            "status": self._readiness_status,
            "phase": self._readiness_phase,
            "ready": ready,
            "elapsed_ms": int(
                max(0.0, time.perf_counter() - self._readiness_started_at) * 1000
            ),
            "degraded_reasons": list(self._readiness_degraded_reasons),
            "failure_code": self._readiness_failure_code,
        }

    async def await_ready(self, timeout: float = _AGENT_READINESS_TIMEOUT_S) -> Dict[str, Any]:
        if not self._readiness_event.is_set():
            try:
                await asyncio.wait_for(self._readiness_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                status = self.get_readiness_status()
                return {
                    **status,
                    "status": "timeout",
                    "ready": False,
                    "failure_code": "agent_readiness_timeout",
                }
        return self.get_readiness_status()

    async def _initialize_capabilities(self) -> None:
        try:
            await self._init_skills_and_tools()
            if self._readiness_degraded_reasons:
                self._readiness_status = "degraded"
                self._set_readiness_phase("degraded")
            else:
                self._readiness_status = "ready"
                self._set_readiness_phase("ready")
        except asyncio.CancelledError:
            self._readiness_status = "failed"
            self._readiness_failure_code = "initialization_cancelled"
            self._set_readiness_phase("failed")
            raise
        except Exception:
            self._readiness_status = "failed"
            self._readiness_failure_code = "required_capability_initialization_failed"
            self._set_readiness_phase("failed")
            logger.exception("Required agent capability initialization failed")
        finally:
            self._readiness_event.set()

    async def _init_skills_and_tools(self) -> None:
        """Background: discover skills, build tool definitions, then warm up slow services."""
        self._set_readiness_phase("skills")
        await asyncio.to_thread(self.skill_registry.discover_and_load)
        self._set_readiness_phase("subagents")
        await asyncio.to_thread(self.subagent_registry.discover_and_load)
        asyncio.create_task(self._cleanup_persisted_histories())
        asyncio.create_task(self._reap_temp_voice_files_loop())

        # Initialize MCP servers if available
        self._set_readiness_phase("mcp")
        try:
            from core.mcp_client import get_mcp_manager

            await asyncio.wait_for(get_mcp_manager().initialize(), timeout=8.0)
        except Exception as e:
            logger.error(f"Failed to initialize MCP servers: {e}")
            self._add_readiness_degradation("mcp_unavailable")

        self._set_readiness_phase("tools")
        self._refresh_tool_definitions()
        # Pre-initialize LanceDB and HTTP connection pools so the first user
        # message doesn't pay the cold-start penalty.
        asyncio.create_task(self._warm_up_services())

    async def _warm_up_services(self) -> None:

        if self._warmed:
            return
        self._warmed = True

        logger.info("🔥 Warming up services…")

        try:
            await self.vector_service._ensure_init()
            logger.info("✅ LanceDB pre-initialized.")
        except Exception as e:
            logger.warning(f"⚠ LanceDB warmup failed (non-critical): {e}")

        try:
            emb = await self.vector_service._get_embedding("hi")
            if emb is not None:
                logger.info("✅ Embedding API connection warmed.")
            else:
                logger.warning(
                    "⚠ Embedding warmup failed (check API keys). Using keyword fallback."
                )
        except Exception as e:
            if prompt_module.is_setup_complete():
                logger.warning(f"⚠ Embedding warmup error (non-critical): {e}")
            else:
                logger.debug(f"Embedding warmup skipped/failed during setup: {e}")

        # 3. Warm the chat LLM HTTP connection. OpenAI Responses models reject
        # max_output_tokens values below 16, so use that cross-provider floor.
        try:
            provider = self.llm_client.resolve_provider(
                self.primary_model,
                default_base_url=self.config.llm.base_url,
            )
            await self.llm_client.complete(
                provider,
                ChatRequest(
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=_LLM_WARMUP_MAX_TOKENS,
                    session_id="__warmup__",
                ),
            )
            if provider.is_codex:
                logger.info("âœ… Codex connection warmed.")
                return
            logger.info("âœ… LLM connection pool warmed.")
            return
        except Exception as e:
            # Warmup failure is never fatal — log and continue
            if prompt_module.is_setup_complete():
                logger.warning(f"⚠ LLM warmup failed (non-critical): {e}")
            else:
                logger.debug(f"LLM warmup skipped/failed during setup: {e}")

    def _refresh_tool_definitions(self) -> None:
        """Rebuild and cache tool definitions. Call only when skills change."""
        self._tool_definitions = self.toolbox.get_tool_definitions()
        self._stable_prompt_cache.clear()
        if hasattr(self, "tool_cache"):
            self.tool_cache.clear()
        self._capability_snapshot_revision = int(
            getattr(getattr(self, "skill_registry", None), "capability_revision", 0)
            or 0
        )
        self._mcp_snapshot_signature = self._get_mcp_snapshot_signature()

        logger.debug(
            f"Tool definitions refreshed ({len(self._tool_definitions)} tools)."
        )

    @staticmethod
    def _get_mcp_snapshot_signature() -> Tuple[Any, ...]:
        try:
            from core.mcp_client import get_mcp_manager

            manager = get_mcp_manager()
            tool_names = tuple(
                sorted(
                    str(tool.get("function", {}).get("name") or "")
                    for tool in (manager.get_tools() or [])
                    if isinstance(tool, dict)
                )
            )
            status = tuple(sorted((manager.get_status() or {}).items()))
            return tool_names, status
        except Exception:
            return ()

    def _get_tool_definitions(self) -> List[Dict]:
        skill_revision = int(
            getattr(getattr(self, "skill_registry", None), "capability_revision", 0)
            or 0
        )
        mcp_signature = self._get_mcp_snapshot_signature()
        snapshot_revision = int(
            getattr(self, "_capability_snapshot_revision", 0) or 0
        )
        if (
            self._tool_definitions is None
            or (
                snapshot_revision > 0
                and skill_revision != snapshot_revision
            )
            or (
                snapshot_revision > 0
                and mcp_signature != getattr(self, "_mcp_snapshot_signature", ())
            )
        ):
            self._refresh_tool_definitions()
        return self._tool_definitions

    @staticmethod
    def _is_capability_followup(text: str) -> bool:
        lowered = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not lowered:
            return True
        return bool(
            re.search(
                r"\b(?:yes|yeah|yep|ok|okay|sure|continue|go ahead|do it|that one|"
                r"same|proceed|sí|si|claro|dale|esa|ese|esa misma|la tienes|"
                r"adelante|hazlo|continúa|continua)\b",
                lowered,
            )
        )

    def _get_capability_turn_context(
        self, session_key: Optional[str], user_text: str
    ) -> Dict[str, Any]:
        """Resolve sticky skill names and the text used for relevance routing."""
        key = str(session_key or "").strip()
        registry = getattr(self, "skill_registry", None)
        if registry is None or not hasattr(registry, "get_relevant_skill_names"):
            return {
                "routing_text": str(user_text or ""),
                "skill_names": [],
                "last_substantive_message": "",
                "revision": 0,
            }

        prior = dict(getattr(self, "_session_capability_state", {}).get(key) or {})
        revision = int(getattr(registry, "capability_revision", 0) or 0)
        if prior.get("revision") != revision:
            prior = {}
        current = str(user_text or "").strip()
        current_names = registry.get_relevant_skill_names(current, max_skills=3)
        previous_names = list(prior.get("skill_names") or [])
        previous_message = str(prior.get("last_substantive_message") or "").strip()
        preserve = bool(previous_names) and (
            self._is_capability_followup(current) or not current_names
        )
        routing_text = current
        if preserve and previous_message:
            routing_text = f"{current}\n{previous_message}".strip()
        selected_names = registry.get_relevant_skill_names(
            routing_text,
            max_skills=3,
            sticky_skill_names=previous_names if preserve else None,
        )
        substantive = bool(current) and not self._is_capability_followup(current)
        last_substantive = current if substantive else previous_message
        state = {
            "skill_names": selected_names,
            "last_substantive_message": last_substantive,
            "revision": revision,
        }
        if key:
            getattr(self, "_session_capability_state", {})[key] = state
        return {"routing_text": routing_text, **state}

    def _capability_catalog_prompt(self) -> str:
        registry = getattr(self, "skill_registry", None)
        catalog: list[Dict[str, Any]] = []
        if registry is not None and hasattr(registry, "get_capability_catalog"):
            try:
                catalog.extend(registry.get_capability_catalog(include_inactive=True))
            except Exception:
                pass
        try:
            for tool in self._get_tool_definitions():
                function = tool.get("function") if isinstance(tool, dict) else {}
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "").strip()
                if not name:
                    continue
                description = redact_sensitive_text(
                    re.sub(r"\s+", " ", str(function.get("description") or "")).strip()
                )[:140]
                state = "ready"
                kind = "mcp_tool" if name.startswith("mcp_") else "native_tool"
                if name.startswith("mcp_"):
                    try:
                        from core.mcp_client import get_mcp_manager

                        server = name.split("_", 2)[1]
                        state = str(
                            get_mcp_manager().get_status().get(server, "Offline")
                        ).lower()
                    except Exception:
                        state = "unknown"
                catalog.append(
                    {
                        "name": name,
                        "type": kind,
                        "description": description,
                        "state": state,
                    }
                )
        except Exception:
            pass
        try:
            from core.mcp_client import get_mcp_manager

            catalog.extend(
                {
                    "name": str(name),
                    "type": "mcp_server",
                    "description": "Configured MCP server",
                    "state": str(state).lower(),
                }
                for name, state in sorted(
                    (get_mcp_manager().get_status() or {}).items()
                )
            )
        except Exception:
            pass
        try:
            descriptions = self.subagent_registry.get_agent_descriptions()
            catalog.extend(
                {
                    "name": str(name),
                    "type": "subagent",
                    "description": redact_sensitive_text(
                        re.sub(r"\s+", " ", str(description or "")).strip()
                    )[:140],
                    "state": "ready",
                }
                for name, description in sorted((descriptions or {}).items())
            )
        except Exception:
            pass
        if not catalog:
            return "\n--- CAPABILITY INVENTORY ---\nNo capabilities are currently discovered. Use `capability_search` before claiming an integration is unavailable.\n"
        lines = [
            "\n--- CAPABILITY INVENTORY ---",
            "Compact discovery snapshot (not full manuals). `ready` means a native tool is registered or a skill is enabled with declared dependencies present; it does not prove external credentials are connected.",
        ]
        for item in catalog[:32]:
            name = str(item.get("name") or "")
            kind = str(item.get("type") or "capability")
            state = str(item.get("state") or "unknown")
            desc = str(item.get("description") or "").strip()
            required = ", ".join(item.get("required_tools") or [])
            suffix = f"; tools: {required}" if required else ""
            lines.append(f"- `{name}` ({kind}) [{state}]: {desc}{suffix}")
        if len(catalog) > 32:
            lines.append(f"- ... {len(catalog) - 32} more; use `capability_search` to resolve by name/task.")
        lines.append(
            "Before saying a requested capability is missing, call `capability_search` with the user's exact task or integration name."
        )
        return "\n".join(lines) + "\n"

    def _get_tool_definitions_for_turn(
        self,
        user_text: str = "",
        forced_skill_name: Optional[str] = None,
        session_key: Optional[str] = None,
    ) -> List[Dict]:
        all_tools = self._get_tool_definitions()
        skill_registry = getattr(self, "skill_registry", None)
        capability_context = self._get_capability_turn_context(session_key, user_text)
        selected_skill_names = list(capability_context.get("skill_names") or [])
        if forced_skill_name and forced_skill_name not in selected_skill_names:
            selected_skill_names.insert(0, forced_skill_name)
        if skill_registry is not None:
            if hasattr(skill_registry, "get_required_tool_names_for_skills"):
                required_tool_names = skill_registry.get_required_tool_names_for_skills(
                    selected_skill_names
                )
            else:
                required_tool_names = []
                for name in selected_skill_names:
                    required_tool_names.extend(
                        skill_registry.get_required_tool_names(name)
                    )
                required_tool_names = list(dict.fromkeys(required_tool_names))
        else:
            required_tool_names = []
        if selected_skill_names and any(
            str(tool.get("function", {}).get("name") or "") == "capability_search"
            for tool in all_tools
            if isinstance(tool, dict)
        ):
            if "capability_search" not in required_tool_names:
                required_tool_names.append("capability_search")
        if self._tool_shortlist_enabled():
            selected = shortlist_tool_definitions(
                all_tools,
                user_text,
                required_tool_names=required_tool_names,
            )
            strategy = "shortlist"
        else:
            selected = list(all_tools)
            strategy = "full_schema_default"

        all_names = self._tool_definition_names(all_tools)
        selected_names = self._tool_definition_names(selected)
        self._log_tool_debug(
            "tool_schema_selection",
            strategy=strategy,
            user_text=user_text,
            total_tool_count=len(all_names),
            total_tools=all_names,
            selected_tool_count=len(selected_names),
            selected_tools=selected_names,
            forced_skill=forced_skill_name,
            matched_skills=selected_skill_names,
            required_skill_tools=required_tool_names,
        )
        return selected

    def _filter_tool_definitions_for_subagent(
        self,
        tool_names: Optional[List[str]],
        disallowed_tool_names: Optional[List[str]] = None,
    ) -> List[Dict]:
        tools = list(self._get_tool_definitions())
        if tool_names is not None:
            allowed = {
                normalize_subagent_tool_name(name)
                for name in tool_names
                if normalize_subagent_tool_name(name)
            }
            tools = [
                tool
                for tool in tools
                if tool.get("function", {}).get("name") in allowed
            ]

        if disallowed_tool_names:
            disallowed = {
                normalize_subagent_tool_name(name)
                for name in disallowed_tool_names
                if normalize_subagent_tool_name(name)
            }
            tools = [
                tool
                for tool in tools
                if tool.get("function", {}).get("name") not in disallowed
            ]

        return tools

    def _resolve_provider(
        self,
    ) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        """
        Return (model, base_url, api_key, custom_llm_provider).
        Called once at init; call again via set_model() when the model changes.
        """
        provider = self.llm_client.resolve_provider(
            self.model,
            default_base_url=self.config.llm.base_url,
        )
        return (
            provider.model,
            provider.base_url,
            provider.api_key,
            provider.custom_llm_provider,
        )

    def _resolve_provider_chain(
        self,
    ) -> List[Tuple[str, str, Optional[str], Optional[str], Optional[str]]]:
        chain = self.llm_client.resolve_chain(
            self.model,
            getattr(self, "fallback_models", []) or [],
            default_base_url=self.config.llm.base_url,
        )
        return [
            (
                provider.source_model,
                provider.model,
                provider.base_url,
                provider.api_key,
                provider.custom_llm_provider,
            )
            for provider in chain
        ]

    def _get_provider_circuit_breaker(self) -> ProviderCircuitBreaker:
        """Lazily provide a breaker for lightweight test/embedded instances."""
        breaker = getattr(self, "provider_circuit_breaker", None)
        if breaker is None:
            breaker = ProviderCircuitBreaker()
            self.provider_circuit_breaker = breaker
        return breaker

    @staticmethod
    def _provider_circuit_key(
        source_model: str,
        model: str,
        base_url: Optional[str],
        custom_llm_provider: Optional[str],
    ) -> str:
        return "|".join(
            str(value or "")
            for value in (source_model, model, base_url, custom_llm_provider)
        )

    def _provider_circuit_identity(
        self,
        source_model: str,
        model: str,
        base_url: Optional[str],
        api_key: Optional[str],
        custom_llm_provider: Optional[str],
    ) -> Tuple[str, str]:
        return (
            self._provider_circuit_key(
                source_model, model, base_url, custom_llm_provider
            ),
            ProviderCircuitBreaker.credential_fingerprint(api_key),
        )

    def set_model(self, model: str) -> None:
        """Switch the active model and refresh cached provider config."""
        self.model = model
        self.primary_model = model
        self._provider = self._resolve_provider()
        self._stable_prompt_cache.clear()
        self._image_input_fallback_sessions.clear()

    def get_llm_runtime_status(self) -> Dict[str, Any]:
        return {
            "configured_model": self.primary_model,
            "active_model": self.model,
            "fallback_models": list(self.fallback_models),
            "using_fallback": self.model != self.primary_model,
            "provider_circuits": self._get_provider_circuit_breaker().snapshot(),
        }

    @staticmethod
    def _should_failover_model(error: Exception) -> bool:
        if isinstance(
            error,
            (
                AuthenticationError,
                RateLimitError,
                InternalServerError,
                APIConnectionError,
                ServiceUnavailableError,
            ),
        ):
            return True

        text = str(error or "").lower()
        return any(
            marker in text
            for marker in (
                "incorrect api key",
                "invalid api key",
                "authentication",
                "auth failed",
                "rate limit",
                "service unavailable",
                "connection error",
                "timed out",
                "timeout",
                "model not found",
                "does not exist",
                "not available",
                "provider returned an error",
                "no visible response",
                "overloaded",
                "capacity",
            )
        )

    def _image_input_fallback_key(self, session_key: str) -> str:
        return f"{self.model}::{session_key}"

    def _image_inputs_disabled_for_session(self, session_key: str) -> bool:
        return self._image_input_fallback_key(session_key) in (
            self._image_input_fallback_sessions
        )

    def _disable_image_inputs_for_session(self, session_key: str) -> None:
        self._image_input_fallback_sessions.add(
            self._image_input_fallback_key(session_key)
        )

    @staticmethod
    def _render_text_only_message_content(content: Any) -> str:
        if not isinstance(content, list):
            return str(content or "")

        text_parts: List[str] = []
        image_notes: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = str(item.get("text", "") or "").strip()
                if text:
                    text_parts.append(text)
            elif item.get("type") == "image_url":
                url = str(item.get("image_url", {}).get("url", "") or "").strip()
                note = (
                    "[Image attachment shared, but the current model does not support vision]"
                )
                if url and not url.lower().startswith("data:"):
                    note = f"{note}: {url}"
                image_notes.append(note)

        combined = "\n".join(part for part in text_parts + image_notes if part)
        return combined or "[Image attachment shared, but the current model does not support vision]"

    @staticmethod
    def _join_message_sections(*sections: str) -> str:
        return "\n\n".join(
            section.strip() for section in sections if str(section or "").strip()
        )

    @staticmethod
    def _build_attachment_summary(attachments: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        image_count = 0
        document_count = 0
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            kind = str(attachment.get("kind") or "attachment").strip().lower()
            name = str(attachment.get("name") or kind or "attachment").strip()
            path = str(attachment.get("path") or "").strip()
            mime_type = str(
                attachment.get("mime_type") or attachment.get("mimeType") or ""
            ).strip()

            if attachment.get("kind") == "image":
                image_count += 1
                note = f"[Attached image {image_count}: {name}]"
                if mime_type:
                    note += f" Type: {mime_type}."
                if path:
                    note += f" Saved as `{path}`."
                lines.append(note)
                continue

            document_count += 1
            note = f"[Attached document {document_count}: {name}]"
            if mime_type:
                note += f" Type: {mime_type}."
            if path:
                note += f" Saved as `{path}`."
            if attachment.get("extracted_text"):
                note += " Text was extracted and included below."
            elif attachment.get("extraction_note"):
                note += f" Extraction note: {attachment.get('extraction_note')}."
            lines.append(note)

        if not lines:
            return ""
        header = (
            f"[Discord attachments received: {image_count} image(s), "
            f"{document_count} document(s)]"
        )
        return "\n".join([header] + lines)

    @staticmethod
    def _build_document_attachment_context(attachments: List[Dict[str, Any]]) -> str:
        sections: List[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            if attachment.get("kind") != "document":
                continue

            name = str(attachment.get("name") or "document").strip()
            path = str(attachment.get("path") or "").strip()
            extracted_text = str(attachment.get("extracted_text") or "").strip()
            extraction_note = str(attachment.get("extraction_note") or "").strip()

            parts = [f"[Document attachment: {name}]"]
            if path:
                parts.append(f"Saved as: {path}")
            if extracted_text:
                parts.append("Extracted text:")
                parts.append(extracted_text)
            elif extraction_note:
                parts.append(f"Note: {extraction_note}")

            sections.append("\n".join(parts))

        return "\n\n".join(section for section in sections if section.strip())

    @staticmethod
    def _inline_local_tool_image(
        path_value: str,
        *,
        mime_type: str = "",
        name: str = "",
        source: str = "",
    ) -> Optional[Dict[str, str]]:
        raw_path = str(path_value or "").strip()
        if not raw_path:
            return None

        try:
            cwd = Path.cwd().resolve()
            candidate = Path(raw_path)
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (cwd / candidate).resolve()
            )
            resolved.relative_to(cwd)
        except Exception:
            return None

        if not resolved.exists() or not resolved.is_file():
            return None

        inferred_mime = str(mime_type or "").strip() or (
            mimetypes.guess_type(resolved.name)[0] or ""
        )
        if not inferred_mime.lower().startswith("image/"):
            return None

        try:
            if resolved.stat().st_size > _TOOL_LOCAL_IMAGE_MAX_BYTES:
                return None
            encoded = base64.b64encode(resolved.read_bytes()).decode("ascii")
        except Exception:
            return None

        return {
            "url": f"data:{inferred_mime};base64,{encoded}",
            "name": str(name or resolved.name).strip(),
            "source": str(source or raw_path).strip(),
        }

    @classmethod
    def _extract_local_tool_images(cls, raw_text: str) -> List[Dict[str, str]]:
        raw = str(raw_text or "")
        parsed_candidates: List[Any] = []
        try:
            parsed_candidates.append(json.loads(raw))
        except Exception:
            pass

        if not parsed_candidates:
            for line in raw.splitlines():
                stripped = line.strip()
                if not stripped.startswith("{") or not stripped.endswith("}"):
                    continue
                try:
                    parsed_candidates.append(json.loads(stripped))
                except Exception:
                    continue

        if not parsed_candidates:
            return []

        images: List[Dict[str, str]] = []
        seen_paths: Set[str] = set()

        def _visit(node: Any, source_hint: str = "") -> None:
            if isinstance(node, dict):
                path_value = str(
                    node.get("saved_path")
                    or node.get("path")
                    or node.get("local_path")
                    or node.get("file_path")
                    or ""
                ).strip()
                if path_value and path_value not in seen_paths:
                    seen_paths.add(path_value)
                    image = cls._inline_local_tool_image(
                        path_value,
                        mime_type=str(
                            node.get("mime_type") or node.get("mimeType") or ""
                        ).strip(),
                        name=str(node.get("filename") or node.get("name") or "").strip(),
                        source=source_hint or "tool-downloaded local image",
                    )
                    if image:
                        images.append(image)

                next_source = source_hint
                issue_key = str(node.get("issue_key") or "").strip()
                if issue_key:
                    next_source = f"Jira attachment on {issue_key.upper()}"

                for value in node.values():
                    _visit(value, next_source)
                return

            if isinstance(node, list):
                for item in node:
                    _visit(item, source_hint)

        for parsed in parsed_candidates:
            _visit(parsed)
        return images

    @staticmethod
    def _extract_tool_media_payload(result: Any) -> Tuple[str, List[Dict[str, str]]]:
        raw_text = str(result or "")
        images: List[Dict[str, str]] = []
        summaries: List[str] = []

        cleaned_parts: List[str] = []
        cursor = 0
        while True:
            start = raw_text.find(_TOOL_MEDIA_PAYLOAD_START, cursor)
            if start < 0:
                cleaned_parts.append(raw_text[cursor:])
                break

            cleaned_parts.append(raw_text[cursor:start])
            payload_start = start + len(_TOOL_MEDIA_PAYLOAD_START)
            end = raw_text.find(_TOOL_MEDIA_PAYLOAD_END, payload_start)
            if end < 0:
                cleaned_parts.append(raw_text[start:])
                break

            payload_text = raw_text[payload_start:end].strip()
            try:
                payload = json.loads(payload_text)
            except Exception:
                payload = None

            if isinstance(payload, dict):
                summary = str(
                    payload.get("text") or payload.get("summary") or ""
                ).strip()
                if summary:
                    summaries.append(summary)

                for item in payload.get("images") or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url") or "").strip()
                    name = str(item.get("name") or "").strip()
                    source = str(item.get("source") or "").strip()
                    if url:
                        images.append(
                            {
                                "url": url,
                                "name": name,
                                "source": source,
                            }
                        )
                        continue

                    path = str(
                        item.get("saved_path")
                        or item.get("path")
                        or item.get("local_path")
                        or item.get("file_path")
                        or ""
                    ).strip()
                    if path:
                        image = AgentLoop._inline_local_tool_image(
                            path,
                            mime_type=str(
                                item.get("mime_type") or item.get("mimeType") or ""
                            ).strip(),
                            name=name,
                            source=source or "tool-downloaded local image",
                        )
                        if image:
                            images.append(image)

                if summary:
                    cleaned_parts.append(summary)
            else:
                cleaned_parts.append(raw_text[start : end + len(_TOOL_MEDIA_PAYLOAD_END)])

            cursor = end + len(_TOOL_MEDIA_PAYLOAD_END)

        cleaned = "".join(cleaned_parts)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

        if not images:
            images.extend(AgentLoop._extract_local_tool_images(cleaned or raw_text))

        if not cleaned and summaries:
            cleaned = "\n".join(summaries)
        if not cleaned and images:
            cleaned = f"Tool returned {len(images)} image attachment(s) for inspection."

        deduped_images: List[Dict[str, str]] = []
        seen_urls: Set[str] = set()
        for image in images:
            url = image.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            deduped_images.append(image)

        return cleaned, deduped_images

    @staticmethod
    def _build_tool_image_followup_content(
        function_name: str, images: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        selected = images[:3]
        names = [image.get("name", "").strip() for image in selected if image.get("name")]
        intro = (
            f"Internal tool-fetched image context from `{function_name}`. "
            "These images were retrieved during tool execution for the current task. "
            "Inspect them directly with your own vision before continuing. "
            "Do not treat this as a new user request, and do not call OCR or "
            "file-reading tools unless the user specifically asks for extraction "
            "or the image is unreadable."
        )
        if names:
            intro += f" Retrieved: {', '.join(names)}."
        if len(images) > len(selected):
            intro += f" Showing {len(selected)} of {len(images)} images."

        content: List[Dict[str, Any]] = [{"type": "text", "text": intro}]
        for image in selected:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image["url"]},
                }
            )
        return content

    @staticmethod
    def _should_suppress_web_final_reply(
        *,
        channel: str,
        any_tool_calls_in_turn: bool,
        iterations_limit_reached: bool,
        web_streamed_reply: bool,
        force_direct_reply: bool,
        reply_to_user: str,
        raw_reply: str,
    ) -> bool:
        if channel != "web":
            return False
        if not any_tool_calls_in_turn:
            return False
        if iterations_limit_reached or not web_streamed_reply or force_direct_reply:
            return False
        normalized_final = AgentLoop._normalize_user_visible_reply_for_compare(
            reply_to_user
        )
        if _GHOST_TAG_RE.search(raw_reply or ""):
            tag_names = "|".join(_GHOST_TAG_NAMES)
            visible_stream = re.sub(
                rf"<(?:{tag_names})[^>]*>.*?</(?:{tag_names})>",
                "",
                raw_reply,
                flags=re.DOTALL | re.IGNORECASE,
            )
            visible_stream = _GHOST_TAG_RE.sub("", visible_stream)
            normalized_visible_stream = re.sub(
                r"\s+", " ", visible_stream
            ).strip()
            return bool(
                normalized_final
                and normalized_visible_stream.count(normalized_final) >= 2
            )
        normalized_streamed = AgentLoop._normalize_user_visible_reply_for_compare(
            raw_reply
        )
        if not normalized_final or not normalized_streamed:
            return False
        return (
            normalized_final == normalized_streamed
            or normalized_final in normalized_streamed
        )

    @staticmethod
    def _resolve_voice_delivery(
        channel: str, voice_cfg: Dict[str, Any], voice_on: bool, has_reply: bool
    ) -> str:
        """Decide how a reply is delivered given the voice config.

        Returns one of: "audio_only", "audio_and_text" (Discord/WhatsApp),
        "web_url" (attach a playable URL to the web text), or "text".
        """
        if not (voice_on and has_reply):
            return "text"
        channels = voice_cfg.get("channels", ["web"])
        if channel in ("discord", "whatsapp") and channel in channels:
            return (
                "audio_and_text"
                if voice_cfg.get("send_text_with_audio", False)
                else "audio_only"
            )
        if channel == "web" and "web" in channels:
            return "web_url"
        return "text"

    async def _reap_temp_voice_files_loop(self) -> None:
        """Periodically delete stale synthesized voice mp3s under temp/.

        Discord/WhatsApp voice sends clean up immediately after sending, but
        the web channel's voice_url leaves a streamed file with no signal for
        when the browser is done playing it. This is that TTL-based cleanup.
        """
        from core.tts import ElevenLabsTTS

        while True:
            try:
                await asyncio.to_thread(ElevenLabsTTS.purge_stale_audio)
            except Exception as e:
                logger.debug(f"[TTS] Temp voice file reaper skipped a pass: {e}")
            await asyncio.sleep(1800)  # every 30 minutes

    async def _synthesize_voice_file(self, text: str) -> Optional[str]:
        """Return a temp mp3 path for `text` if voice is enabled and keyed, else None."""
        try:
            from core.tts import ElevenLabsTTS

            cfg = ElevenLabsTTS.get_voice_config()
            if not cfg.get("enabled", False) or not ElevenLabsTTS.get_api_key():
                return None
            path = await ElevenLabsTTS.synthesize_to_file(text)
            return path or None
        except Exception as e:
            logger.error(f"[TTS] Voice synthesis failed: {e}")
            return None

    @staticmethod
    def _messages_have_image_inputs(messages: List[Dict[str, Any]]) -> bool:
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    return True
        return False

    @staticmethod
    def _collect_image_inputs(
        attachments: List[Dict[str, Any]],
        metadata_images: Any = None,
        legacy_image: str = "",
    ) -> List[str]:
        """Prefer inline image data without adding its CDN URL a second time."""
        image_inputs: List[str] = []
        represented_urls: set[str] = set()

        for attachment in attachments:
            if attachment.get("kind") != "image":
                continue
            remote_url = str(attachment.get("url") or "").strip()
            preferred = str(
                attachment.get("data_url") or remote_url or ""
            ).strip()
            if preferred and preferred not in image_inputs:
                image_inputs.append(preferred)
            if remote_url:
                represented_urls.add(remote_url)

        extra_images = (
            [metadata_images]
            if isinstance(metadata_images, str)
            else list(metadata_images or [])
        )
        for candidate in [*extra_images, legacy_image]:
            value = str(candidate or "").strip()
            if value and value not in represented_urls and value not in image_inputs:
                image_inputs.append(value)

        return image_inputs

    def _remember_image_attachments(
        self, session_key: str, attachments: List[Dict[str, Any]]
    ) -> None:
        images = [
            dict(attachment)
            for attachment in attachments
            if attachment.get("kind") == "image"
            and str(attachment.get("path") or "").strip()
        ][:4]
        if images:
            self._recent_image_attachments[session_key] = (time.time(), images)

    def _get_recent_image_attachments(
        self, session_key: str
    ) -> List[Dict[str, Any]]:
        remembered = self._recent_image_attachments.get(session_key)
        if not remembered:
            return []
        remembered_at, attachments = remembered
        if time.time() - remembered_at > _RECENT_IMAGE_REFERENCE_TTL_S:
            self._recent_image_attachments.pop(session_key, None)
            return []
        usable = [
            dict(attachment)
            for attachment in attachments
            if str(attachment.get("path") or "").strip()
            and Path(str(attachment.get("path"))).is_file()
        ]
        if not usable:
            self._recent_image_attachments.pop(session_key, None)
        return usable

    @staticmethod
    def _message_refers_to_recent_image(content: str) -> bool:
        return bool(
            re.search(
                r"\b(?:image|photo|picture|attachment|reference|same|this|that|"
                r"imagen|foto|adjunt[oa]|referencia|misma|esta|esa|"
                r"generate\s+it|make\s+it|generala|gen[eé]rala|creala|cr[eé]ala|hazla)\b",
                str(content or ""),
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _image_safe_history_content(content: Any) -> Any:
        """Replace image bytes and expiring URLs with a durable small marker."""
        if not isinstance(content, list):
            return content
        text_parts = [
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        image_count = sum(
            1
            for item in content
            if isinstance(item, dict) and item.get("type") == "image_url"
        )
        if not image_count:
            return content
        marker = (
            "[Image attachment processed]"
            if image_count == 1
            else f"[{image_count} image attachments processed]"
        )
        return " ".join([part for part in text_parts if part] + [marker]).strip()

    @classmethod
    def _history_summary_payload(cls, messages: List[Dict[str, Any]]) -> str:
        """Serialize history for summarization without embedding image payloads."""
        safe_messages = []
        for message in messages:
            safe_message = dict(message)
            safe_message["content"] = cls._image_safe_history_content(
                message.get("content")
            )
            safe_messages.append(safe_message)
        return json.dumps(safe_messages, ensure_ascii=False, default=str)

    @staticmethod
    def _tool_call_id(tool_call: Any) -> str:
        """Return the stable provider ID from a dict or SDK tool-call object."""
        if isinstance(tool_call, dict):
            return str(
                tool_call.get("id") or tool_call.get("tool_call_id") or ""
            ).strip()
        return str(
            getattr(tool_call, "id", None)
            or getattr(tool_call, "tool_call_id", None)
            or ""
        ).strip()

    @classmethod
    def _normalize_tool_history_messages(
        cls,
        messages: List[Dict[str, Any]],
        *,
        tool_output_limit: int = _HISTORY_TOOL_OUTPUT_MAX_CHARS,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Repair tool-call/result pairing and cap persisted tool output.

        Providers require every assistant ``tool_calls`` message to be followed
        by one tool result per call.  A process crash, cancellation, or partial
        history write can violate that contract and make every subsequent turn
        fail before LimeBot can recover.  This normalizer inserts deterministic
        synthetic results for missing calls, drops orphan tool rows, and keeps
        all tool content within a bounded character budget.
        """
        normalized: List[Dict[str, Any]] = []
        pending: Dict[str, str] = {}
        changed = False

        def flush_pending() -> None:
            nonlocal changed
            if not pending:
                return
            for tool_call_id, tool_name in pending.items():
                normalized.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name or "tool",
                        "content": _HISTORY_INTERRUPTED_TOOL_MESSAGE,
                    }
                )
            pending.clear()
            changed = True

        for original in messages or []:
            if not isinstance(original, dict):
                changed = True
                continue

            role = str(original.get("role") or "").strip().lower()
            if role == "assistant" and isinstance(original.get("tool_calls"), list):
                # A new assistant turn closes any previous incomplete batch.
                flush_pending()
                message = dict(original)
                valid_calls: List[Dict[str, Any]] = []
                for raw_call in original.get("tool_calls") or []:
                    if isinstance(raw_call, dict):
                        tool_call = dict(raw_call)
                    elif hasattr(raw_call, "model_dump"):
                        tool_call = raw_call.model_dump()
                    else:
                        continue
                    tool_call_id = cls._tool_call_id(tool_call)
                    if not tool_call_id or tool_call_id in pending:
                        changed = True
                        continue
                    function = tool_call.get("function")
                    tool_name = (
                        function.get("name")
                        if isinstance(function, dict)
                        else tool_call.get("name")
                    )
                    pending[tool_call_id] = str(tool_name or "tool")
                    valid_calls.append(tool_call)

                if valid_calls:
                    message["tool_calls"] = valid_calls
                else:
                    message.pop("tool_calls", None)
                    if original.get("tool_calls"):
                        changed = True
                if message != original:
                    changed = True
                normalized.append(message)
                continue

            if role == "tool":
                tool_call_id = str(original.get("tool_call_id") or "").strip()
                if not tool_call_id or tool_call_id not in pending:
                    # Orphan/duplicate tool rows are invalid upstream and are
                    # safer to discard than to let them poison the next call.
                    changed = True
                    continue
                message = dict(original)
                bounded_content = truncate_tool_result(
                    message.get("content", ""), tool_output_limit
                )
                if bounded_content != str(message.get("content", "")):
                    changed = True
                message["content"] = bounded_content
                normalized.append(message)
                pending.pop(tool_call_id, None)
                continue

            flush_pending()
            normalized.append(original)

        flush_pending()
        return normalized, changed

    def _normalize_history_in_place(self, session_key: str) -> List[Dict[str, Any]]:
        """Normalize and persist the in-memory history for one session."""
        history = self.history.get(session_key, [])
        normalized, changed = self._normalize_tool_history_messages(history)
        if changed:
            self.history[session_key] = normalized
            self._mark_dirty(session_key)
        return self.history.get(session_key, normalized)

    def _evict_history_image_inputs(self, session_key: str) -> None:
        changed = False
        for message in self.history.get(session_key, []):
            original_content = message.get("content")
            safe_content = self._image_safe_history_content(original_content)
            if safe_content is not original_content:
                message["content"] = safe_content
                changed = True
        if changed:
            self._mark_dirty(session_key)

    def _truncate_history_fallback(
        self, conv: List[Dict[str, Any]], target_tokens: int
    ) -> List[Dict[str, Any]]:
        """Trim old turns without deleting the latest user request."""
        current_tokens = self._estimate_tokens(conv)

        while conv and current_tokens > target_tokens:
            latest_user_index = max(
                (
                    index
                    for index, message in enumerate(conv)
                    if message.get("role") == "user"
                ),
                default=len(conv),
            )
            if latest_user_index <= 0:
                break

            popped = conv.pop(0)
            current_tokens -= self._estimate_tokens([popped])

            if popped.get("role") == "assistant" and popped.get("tool_calls"):
                while conv and conv[0].get("role") == "tool":
                    tool_msg = conv.pop(0)
                    current_tokens -= self._estimate_tokens([tool_msg])
            while conv and conv[0].get("role") == "tool":
                tool_msg = conv.pop(0)
                current_tokens -= self._estimate_tokens([tool_msg])

        return conv

    def _downgrade_image_messages_for_text_model(
        self, messages: List[Dict[str, Any]], session_key: str
    ) -> bool:
        changed = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if not any(
                isinstance(item, dict) and item.get("type") == "image_url"
                for item in content
            ):
                continue
            message["content"] = self._render_text_only_message_content(content)
            changed = True

        if changed:
            self.metrics.record_anomaly(
                session_key,
                "image_input_downgraded_for_text_model",
                detail=self.model,
            )
        return changed

    def _should_retry_without_images(
        self, error: Exception, messages: List[Dict[str, Any]]
    ) -> bool:
        if not self._messages_have_image_inputs(messages):
            return False
        error_text = str(error).lower()
        return any(
            phrase in error_text
            for phrase in (
                "not a multimodal model",
                "does not support vision",
                "does not support image",
                "doesn't support vision",
                "doesn't support image",
            )
        )

    async def _get_stable_prompt(
        self, sender_id: str, channel: str, chat_id: str, sender_name: str = ""
    ) -> str:
        """
        Return the rarely-changing part of the system prompt, cached per
        (sender_id, channel) for _STABLE_PROMPT_TTL seconds.
        """
        key = f"{sender_id}:{channel}"
        cached = self._stable_prompt_cache.get(key)
        now = time.monotonic()

        if cached and now < cached[1]:
            return cached[0]

        try:
            soul = (
                await asyncio.to_thread(SOUL_FILE.read_text, encoding="utf-8")
                if SOUL_FILE.exists()
                else ""
            )
            identity_raw = (
                await asyncio.to_thread(IDENTITY_FILE.read_text, encoding="utf-8")
                if IDENTITY_FILE.exists()
                else ""
            )
        except Exception as e:
            logger.warning(f"Error reading persona files: {e}")
            soul = identity_raw = ""

        if not prompt_module.is_setup_complete(
            soul_content=soul, identity_content=identity_raw
        ):
            return prompt_module.get_setup_prompt(
                soul_content=soul, identity_content=identity_raw
            )

        stable = prompt_module.build_stable_system_prompt(
            sender_id=sender_id,
            channel=channel,
            chat_id=chat_id,
            model=self.model,
            allowed_paths=self.toolbox.allowed_paths,
            skill_registry=self.skill_registry,
            config=self.config,
            soul=soul,
            identity_raw=identity_raw,
            sender_name=sender_name,
        )
        self._stable_prompt_cache[key] = (stable, now + self._STABLE_PROMPT_TTL)
        return stable

    def _invalidate_stable_prompt(self, sender_id: str) -> None:
        """Drop cached prompts for this sender. Call after soul/identity updates."""
        for key in [
            k for k in self._stable_prompt_cache if k.startswith(f"{sender_id}:")
        ]:
            del self._stable_prompt_cache[key]

    def _log_session_event(self, session_key: str, event: dict) -> None:
        try:
            payload = dict(event or {})
            task_id = _CURRENT_TASK_ID.get()
            if task_id:
                payload.setdefault("task_id", task_id)
            asyncio.create_task(
                self.session_manager.append_event_log(session_key, payload)
            )
        except Exception:
            pass

    async def _build_full_system_prompt(
        self,
        sender_id: str,
        channel: str,
        chat_id: str,
        recalled_context: str = "",
        sender_name: str = "",
        current_message: str = "",
        forced_skill_name: Optional[str] = None,
        ponytail_mode: str = "off",
        session_key: Optional[str] = None,
    ) -> str:
        """Stable (cached) + volatile (per-message: memory + RAG + timestamp)."""
        stable = await self._get_stable_prompt(sender_id, channel, chat_id, sender_name)
        capability_context = self._get_capability_turn_context(
            session_key, current_message
        )
        routing_text = capability_context.get("routing_text") or current_message
        if forced_skill_name:
            skills_docs = self.skill_registry.get_forced_prompt_addition(
                forced_skill_name
            )
        else:
            skills_docs = self.skill_registry.get_relevant_prompt_additions(
                routing_text
            )
        capability_docs = (
            self._capability_catalog_prompt()
            if prompt_module.is_setup_complete()
            else ""
        )
        subagent_docs = self.subagent_registry.get_prompt_additions(current_message)
        ponytail_docs = build_ponytail_prompt_addition(ponytail_mode)
        include_private_memory = prompt_module.should_load_private_context(
            sender_id, channel, self.config
        )
        volatile = prompt_module.get_volatile_prompt_suffix(
            recalled_context,
            include_private_memory=include_private_memory,
            current_message=current_message,
        )
        return (
            stable
            + capability_docs
            + (skills_docs + "\n" if skills_docs else "")
            + (subagent_docs + "\n" if subagent_docs else "")
            + (ponytail_docs + "\n" if ponytail_docs else "")
            + volatile
        )

    def _resolve_skill_invocation(
        self, content: str
    ) -> Tuple[str, Optional[str], Optional[str]]:
        parsed = parse_skill_invocation(content)
        if parsed.kind == "none":
            return content, None, None

        if parsed.kind == "inventory":
            return "what skills do you have right now", None, None

        return self._resolve_requested_skill(
            parsed.requested_name,
            parsed.task,
            raw_content=content,
        )

    def _resolve_requested_skill(
        self, requested_name: str, task: str = "", raw_content: Optional[str] = None
    ) -> Tuple[str, Optional[str], Optional[str]]:
        resolved_name = self.skill_registry.resolve_active_skill_name(
            requested_name
        )
        if not resolved_name:
            active_skills = self.skill_registry.list_active_skill_names()
            if active_skills:
                active_text = ", ".join(f"`{name}`" for name in active_skills)
                error = (
                    f"Unknown or inactive skill `{requested_name}`. "
                    f"Active skills: {active_text}."
                )
            else:
                error = (
                    f"Unknown or inactive skill `{requested_name}`. "
                    "No active skills are enabled right now."
                )
            return raw_content if raw_content is not None else task, None, error

        task = task.strip() or f"Use the {resolved_name} skill."
        return task, resolved_name, None

    async def _cleanup_persisted_histories(self) -> None:
        """Best-effort cleanup of malformed assistant residue in persisted sessions."""
        try:
            summary = await asyncio.to_thread(
                self.session_manager.cleanup_history_artifacts
            )
        except Exception as e:
            logger.debug(f"History cleanup skipped: {e}")
            return

        cleaned_total = summary.get("history_entries", 0) + summary.get(
            "log_entries", 0
        )
        if not cleaned_total:
            return

        logger.info(
            "🧹 Cleaned persisted malformed chat residue: "
            f"{summary.get('history_entries', 0)} history entr"
            f"{'y' if summary.get('history_entries', 0) == 1 else 'ies'}, "
            f"{summary.get('log_entries', 0)} log entr"
            f"{'y' if summary.get('log_entries', 0) == 1 else 'ies'}."
        )
        self.metrics.record_anomaly(
            "system",
            "history_cleanup",
            detail=str(summary),
            count=cleaned_total,
        )

    @staticmethod
    def _with_trace_metadata(
        metadata: Optional[Dict[str, Any]] = None,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = dict(metadata or {})
        if turn_id:
            payload.setdefault("turn_id", turn_id)
        if message_id:
            payload.setdefault("message_id", message_id)
        resolved_task_id = task_id or _CURRENT_TASK_ID.get()
        if resolved_task_id:
            payload.setdefault("task_id", resolved_task_id)
        payload.setdefault("event_id", f"evt_{uuid.uuid4().hex[:20]}")
        return payload

    def _normalize_tool_alias(
        self, function_name: str, function_args: dict, session_key: str
    ) -> tuple[str, dict]:
        """Normalize common alias tools back to canonical runtime tool names."""
        return normalize_tool_alias(
            function_name, function_args, self.metrics.record_anomaly, session_key
        )

    # ── Shared publishing helpers ────────────────────────────────────────

    async def _publish(
        self, msg: Optional[InboundMessage], content: str, metadata: dict
    ) -> None:
        """Publish an OutboundMessage to the originating channel (or 'web')."""
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel if msg else "web",
                chat_id=msg.chat_id if msg else "system",
                content=content,
                metadata=metadata,
            )
        )

    async def _publish_both(
        self, msg: Optional[InboundMessage], content: str, metadata: dict
    ) -> None:
        """Publish to the originating channel AND mirror to web if needed."""
        await self._publish(msg, content, metadata)
        if msg and msg.channel != "web":
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel="web",
                    chat_id=msg.chat_id or "system",
                    content=content,
                    metadata=metadata,
                )
            )

    # ── Confirmation embed builder ───────────────────────────────────────

    async def _publish_activity(
        self,
        msg: Optional[InboundMessage],
        text: str,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        await self._publish_both(
            msg,
            "",
            self._with_trace_metadata(
                {"type": "activity", "text": text},
                turn_id=turn_id,
                message_id=message_id,
            ),
        )

    @staticmethod
    def _truncate_preview(text: Any, limit: int = 1200) -> str:
        raw = str(text or "")
        if len(raw) <= limit:
            return raw
        return raw[:limit] + "\n... (truncated)"

    @staticmethod
    def _safe_json_load(raw: Any) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return json.loads(raw)
        except Exception:
            try:
                return ast.literal_eval(raw)
            except Exception:
                return None

    @staticmethod
    def _env_truthy(name: str) -> bool:
        value = str(os.getenv(name, "") or "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _tool_debug_enabled(self) -> bool:
        return self._env_truthy("LIMEBOT_TOOL_DEBUG")

    def _tool_shortlist_enabled(self) -> bool:
        return bool(
            getattr(getattr(self, "config", None), "tool_shortlist_enabled", False)
        )

    def _is_fast_ai_harness_enabled(self) -> bool:
        ai_harness = getattr(getattr(self, "config", None), "ai_harness", None)
        return getattr(ai_harness, "mode", "fast") == "fast"

    @staticmethod
    def _looks_like_action_or_path_request(content: str) -> bool:
        raw = str(content or "").strip()
        if not raw:
            return False
        lowered = raw.lower()
        first_token = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", lowered.split()[0])
        if first_token in _FAST_TOOL_ACTION_VERBS:
            return True
        if lowered.startswith(("/", "@")):
            return True
        if re.search(r"https?://|www\.", raw, re.IGNORECASE):
            return True
        if re.search(r"(?:[A-Za-z]:\\|(?:\./|\.\./|/|\\))", raw):
            return True
        return bool(re.search(r"\b[\w.-]+\.[A-Za-z0-9]{1,8}\b", raw))

    @staticmethod
    def _is_fast_casual_turn(content: str) -> bool:
        raw = str(content or "").strip()
        if not raw:
            return True
        lowered = raw.lower()
        if lowered in _CASUAL_WORDS:
            return True
        if len(raw) <= 10:
            return True
        return any(lowered.startswith(prefix) for prefix in _CASUAL_PHRASE_PREFIXES)

    def _should_include_tools_for_turn(
        self, content: str, session_key: Optional[str] = None
    ) -> bool:
        if not self._should_include_tools(content):
            return False
        active_capability_state = getattr(self, "_session_capability_state", {}).get(
            str(session_key or ""), {}
        )
        lowered = str(content or "").strip().lower()
        explicit_followup = bool(
            re.search(
                r"\b(?:yes|yeah|yep|ok|okay|sure|continue|go ahead|do it|that one|"
                r"same|proceed|sí|si|claro|dale|esa|ese|la tienes|adelante|"
                r"hazlo|continúa|continua)\b",
                lowered,
            )
        )
        if active_capability_state.get("skill_names") and (
            explicit_followup
            or (
                len(lowered) > 10
                and self._is_capability_followup(content)
                and not self._is_fast_casual_turn(content)
            )
        ):
            # A terse acknowledgement still belongs to the active task. Keep
            # schemas (including capability_search) available for verification.
            return True
        if not self._is_fast_ai_harness_enabled():
            return True
        ai_harness = getattr(getattr(self, "config", None), "ai_harness", None)
        if not getattr(ai_harness, "fast_disable_tools_for_casual", True):
            return True
        if self._looks_like_action_or_path_request(content):
            return True
        return not self._is_fast_casual_turn(content)

    @staticmethod
    def _requires_initial_tool_call(
        content: str, tool_definitions: Optional[List[Dict[str, Any]]]
    ) -> bool:
        """Require evidence-producing tool use for explicit external actions."""
        if not str(content or "").strip() or not tool_definitions:
            return False
        tool_names = {
            str(tool.get("function", {}).get("name", ""))
            for tool in tool_definitions
        }
        if "capability_search" in tool_names and re.search(
            r"\b(?:capability|capabilities|integration|integrations|mcp|"
            r"connected|connection|unavailable|available|credentials?|credenciales?|"
            r"conectad[oa]s?|conexi[oó]n|disponible)\b",
            str(content or ""),
            re.IGNORECASE,
        ):
            return True
        has_external_tool = bool(
            tool_names
            & {
                "browser_navigate",
                "web_search",
                "deep_research",
                "google_search",
                "run_command",
            }
        )
        if not has_external_tool:
            return False
        raw = str(content)
        if re.search(r"https?://|www\.", raw, re.IGNORECASE):
            return True
        return bool(_EXPLICIT_TOOL_REQUEST_RE.search(raw))

    @staticmethod
    def _build_provider_config(
        source_model: str,
        model: str,
        base_url: Optional[str],
        api_key: Optional[str],
        custom_llm_provider: Optional[str],
    ) -> ProviderConfig:
        return ProviderConfig(
            source_model=source_model,
            model=model,
            base_url=base_url,
            api_key=api_key,
            custom_llm_provider=custom_llm_provider,
            is_codex=str(source_model or "").startswith("openai-codex/"),
        )

    def _record_stage_timing(
        self,
        session_key: str,
        stage: str,
        started_at: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            self.metrics.record_stage_timing(
                session_key,
                stage,
                time.perf_counter() - started_at,
                metadata=metadata,
            )
        except Exception:
            pass

    @staticmethod
    def _tool_definition_names(tool_defs: List[Dict[str, Any]]) -> List[str]:
        names: List[str] = []
        for tool in tool_defs or []:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return names

    def _tool_call_debug_rows(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for tool_call in tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                function = {}
            rows.append(
                {
                    "id": tool_call.get("id"),
                    "name": function.get("name"),
                    "arguments": self._tool_debug_preview(
                        function.get("arguments", ""), limit=240
                    ),
                }
            )
        return rows

    @staticmethod
    def _tool_debug_preview(text: Any, limit: int = 400) -> str:
        raw = redact_sensitive_text(text or "")
        raw = raw.replace("\r", "\\r").replace("\n", "\\n")
        if len(raw) <= limit:
            return raw
        return raw[:limit] + "... (truncated)"

    def _log_tool_debug(self, event: str, **fields: Any) -> None:
        if not self._tool_debug_enabled():
            return

        rendered: Dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, str):
                rendered[key] = self._tool_debug_preview(value, limit=900)
            elif isinstance(value, list):
                if all(
                    isinstance(item, (str, int, float, bool, type(None)))
                    for item in value
                ):
                    items = list(value[:40])
                    rendered[key] = [
                        self._tool_debug_preview(item, limit=160)
                        if isinstance(item, str)
                        else item
                        for item in items
                    ]
                    if len(value) > 40:
                        rendered[key].append(f"... (+{len(value) - 40} more)")
                else:
                    rendered[key] = self._tool_debug_preview(
                        json.dumps(value, ensure_ascii=False, default=str),
                        limit=1600,
                    )
            elif isinstance(value, dict):
                rendered[key] = self._tool_debug_preview(
                    json.dumps(value, ensure_ascii=False, default=str),
                    limit=1600,
                )
            else:
                rendered[key] = value

        logger.info(
            f"[TOOL DEBUG] {event} | "
            f"{json.dumps(rendered, ensure_ascii=False, default=str)}"
        )

    def _record_rag_trace(self, session_key: str, trace: Dict[str, Any]) -> None:
        self.rag.record(session_key, trace)

    def get_recent_rag_traces(
        self, session_key: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        if not getattr(self, "rag", None):
            return []
        return self.rag.get_recent(session_key, limit) or []

    def _build_rag_result_trace(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return self.rag.build_result_trace(row)

    def _build_write_preview(self, function_args: dict) -> Dict[str, Any]:
        return self.confirm.build_write_preview(function_args)

    def _build_delete_preview(self, function_args: dict) -> Dict[str, Any]:
        return self.confirm.build_delete_preview(function_args)

    @staticmethod
    def _clean_subagent_final_result(result: str) -> str:
        text = str(result or "").strip()
        if not text:
            return ""

        filtered_lines: List[str] = []
        meta_prefixes = (
            "the assistant attempted to",
            "need to produce",
            "need to finish",
            "probably need to",
            "provide final response directly",
            "within limits",
            "got truncated due to",
        )

        for raw_line in text.splitlines():
            line = raw_line.strip()
            lower = line.lower()
            if any(lower.startswith(prefix) for prefix in meta_prefixes):
                continue
            filtered_lines.append(raw_line)

        cleaned = "\n".join(filtered_lines).strip()
        if not cleaned:
            return ""
        return cleaned

    @staticmethod
    def _extract_command_paths(command: str, limit: int = 5) -> List[str]:
        from core.confirmation import ConfirmationManager

        return ConfirmationManager.extract_command_paths(command, limit)

    def _build_command_preview(self, function_args: dict) -> Dict[str, Any]:
        return self.confirm.build_command_preview(function_args)

    def _build_confirmation_preview(
        self, function_name: str, function_args: dict, session_key: str
    ) -> Dict[str, Any]:
        return self.confirm.build_preview(function_name, function_args, session_key)

    @staticmethod
    def _session_whitelist_key(
        function_name: str, function_args: Optional[dict] = None
    ) -> str:
        """Session-whitelist key for a tool call.

        For ``run_command`` the key is scoped to the invoked binary
        (``run_command::<binary>``) so that "always allow this session" only
        unblocks the same program instead of every future shell command.
        Falls back to the full command string when parsing fails, and to the
        bare tool name for every other tool.
        """
        if function_name == "run_command" and isinstance(function_args, dict):
            command = str(function_args.get("command") or "").strip()
            if not command:
                return function_name
            binary = command
            try:
                parts = shlex.split(command)
                if parts:
                    binary = parts[0]
            except Exception:
                binary = command
            return f"run_command::{binary}"
        return function_name

    def _get_tool_approval_decision(
        self,
        session_key: str,
        function_name: str,
        is_internal: bool = False,
        is_whatsapp: bool = False,
        function_args: Optional[dict] = None,
    ) -> Dict[str, Any]:
        profile = str(
            getattr(self.config, "approval_policy_profile", "manual") or "manual"
        ).strip().lower()
        if profile not in {"manual", "session", "autonomous", "review"}:
            profile = "manual"

        if is_whatsapp:
            # WhatsApp is an explicitly fast channel. Contact allow-listing and
            # the toolbox's hard safety checks still apply, but the interactive
            # approval UI is not available in the conversation, so sensitive
            # tools execute without a confirmation round-trip.
            return {
                "allowed": True,
                "requires_confirmation": False,
                "reason": "channel_whatsapp_autonomous",
                "policy_profile": profile,
            }
        if is_internal:
            return {
                "allowed": True,
                "requires_confirmation": False,
                "reason": "internal",
                "policy_profile": profile,
            }
        if profile == "autonomous":
            return {
                "allowed": True,
                "requires_confirmation": False,
                "reason": "policy_autonomous",
                "policy_profile": profile,
            }
        if profile != "review" and self._session_whitelist_key(
            function_name, function_args
        ) in self.session_whitelists.get(session_key, set()):
            return {
                "allowed": True,
                "requires_confirmation": False,
                "reason": "session_whitelist",
                "policy_profile": profile,
            }
        return {
            "allowed": False,
            "requires_confirmation": True,
            "reason": "manual_required",
            "policy_profile": profile,
        }

    @staticmethod
    def _approval_audit_preview(preview: Dict[str, Any]) -> Dict[str, Any]:
        """Keep audit metadata useful without persisting commands or file contents."""
        safe: Dict[str, Any] = {"kind": str(preview.get("kind") or "unknown")}
        risk_flags = preview.get("risk_flags")
        if isinstance(risk_flags, list):
            safe["risk_flags"] = [str(flag)[:80] for flag in risk_flags[:10]]
        for key in ("mode", "target_type"):
            if preview.get(key):
                safe[key] = str(preview[key])[:80]
        affected_paths = preview.get("affected_paths")
        if isinstance(affected_paths, list):
            safe["affected_path_count"] = len(affected_paths)
        return safe

    def _build_confirmation_embed(
        self,
        function_name: str,
        function_args: dict,
        session_key: str,
        preview: Optional[Dict[str, Any]] = None,
    ) -> list:
        """Build the embed fields list for a tool confirmation prompt."""
        return self.confirm.build_embed(
            function_name, function_args, session_key, preview
        )

    # ── Stream result unpacker ───────────────────────────────────────────

    @staticmethod
    def _unpack_stream_result(result) -> tuple:
        """Unpack _consume_stream result into content, calls, usage, web-streamed, discord-streamed."""
        if isinstance(result, tuple) and len(result) >= 5:
            return result[0], result[1], result[2], result[3], result[4]
        if isinstance(result, tuple) and len(result) >= 4:
            return result[0], result[1], result[2], result[3], False
        content, tool_calls, usage = result
        return content, tool_calls, usage, False, False

    # ── Overlap deduplication ────────────────────────────────────────────

    @staticmethod
    def _dedup_overlap(accumulated: str, new_content: str) -> str:
        """Return the portion of *new_content* that doesn't overlap with the
        tail of *accumulated*.  Used after tool-call continuations where the
        LLM may repeat previously-streamed text.

        Two-stage approach:
          A) Literal tail-prefix overlap (extended window up to 300 chars).
          B) Paragraph-level dedup — drops paragraphs from *new_content*
             that already appear in *accumulated*.
        """
        acc_s, nxt_s = accumulated.strip(), new_content.strip()
        if not (acc_s and nxt_s):
            return new_content

        # Stage A: tail-prefix overlap (extended window)
        max_overlap = min(len(acc_s), len(nxt_s), 300)
        for length in range(max_overlap, 4, -1):
            suffix = acc_s[-length:]
            if nxt_s.lower().startswith(suffix.lower()):
                match = re.search(re.escape(suffix), new_content, re.IGNORECASE)
                if match:
                    clean = new_content[match.start() + length :].lstrip()
                    return clean if clean.strip() else ""

        # Stage B: paragraph-level dedup
        acc_paras = [p.strip() for p in re.split(r"\n\s*\n", acc_s) if p.strip()]
        nxt_paras = [p.strip() for p in re.split(r"\n\s*\n", nxt_s) if p.strip()]

        if not nxt_paras:
            return ""

        # Build a set of normalized accumulated paragraphs for fast lookup
        acc_set = {p.lower() for p in acc_paras}

        # Keep only paragraphs from new_content that are genuinely new
        novel = [p for p in nxt_paras if p.lower() not in acc_set]

        # If everything was a duplicate, return empty
        if not novel:
            return ""

        # If some but not all were dupes, return only the novel ones
        if len(novel) < len(nxt_paras):
            return "\n\n".join(novel)

        return new_content

    @staticmethod
    def _dedupe_repeated_reply_sections(reply_to_user: str) -> str:
        if not reply_to_user:
            return reply_to_user

        # 1. Exact-half string repetition (glued repeat)
        n_chars = len(reply_to_user)
        if n_chars > 80 and n_chars % 2 == 0:
            half = n_chars // 2
            if reply_to_user[:half] == reply_to_user[half:]:
                logger.info("✂ Self-repetition detected (glued) — trimming duplicate half.")
                return reply_to_user[:half]

        # 2. Paragraph-level repetition
        if len(reply_to_user) > 80:
            _paras = [
                p.strip()
                for p in re.split(r"\n\s*\n", reply_to_user)
                if p.strip()
            ]
            _n = len(_paras)
            # Exact-half repetition (even paragraph count)
            if _n >= 4 and _n % 2 == 0:
                if _paras[: _n // 2] == _paras[_n // 2 :]:
                    logger.info(
                        "✂ Self-repetition detected — trimming duplicate."
                    )
                    return "\n\n".join(_paras[: _n // 2])
            # Trailing-repeat (odd count or partial overlap)
            if _n >= 3:
                _half = _n // 2
                if _half >= 2 and _paras[:_half] == _paras[-_half:]:
                    logger.info(
                        "✂ Self-repetition detected (trailing) — trimming."
                    )
                    return "\n\n".join(_paras[: _n - _half])

        return reply_to_user

    @staticmethod
    def _normalize_user_visible_reply_for_compare(value: str) -> str:
        if not value:
            return ""

        cleaned = _GHOST_TAG_RE.sub("", str(value))
        cleaned = re.sub(
            r"<(?:" + "|".join(_GHOST_TAG_NAMES) + r")[^>]*>.*?</(?:"
            + "|".join(_GHOST_TAG_NAMES) + r")>",
            "",
            cleaned,
            flags=re.DOTALL,
        )
        cleaned = AgentLoop._dedupe_repeated_reply_sections(cleaned.strip())
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _estimate_tokens(messages: List[Dict]) -> int:
        """
        O(n) token estimate (~4 chars per token, slightly conservative).
        Used in the pruning loop to avoid O(n²) token_counter calls.
        """
        text_chars = 0
        image_tokens = 0
        for m in messages:
            c = m.get("content", "")
            if not isinstance(c, list):
                text_chars += len(str(c))
                continue
            for item in c:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    # Image inputs are billed by dimensions/detail, not by the
                    # character length of a base64 data URL.
                    image_tokens += 1_024
                else:
                    text_chars += len(str(item))
        return (text_chars // 4) + image_tokens

    def _get_task_registry(self) -> ManagedTaskRegistry:
        """Return the live registry, including for lightweight test doubles."""
        registry = getattr(self, "task_registry", None)
        if registry is None:
            registry = ManagedTaskRegistry()
            self.task_registry = registry
        return registry

    async def _persist_managed_task_terminal(self, entry: Any) -> None:
        """Close the durable task projection when a live handle is cancelled early."""
        from core.task_tracker import TaskStatus, get_task_tracker

        if self.active_tasks.get(entry.session_key) is entry.handle:
            self.active_tasks.pop(entry.session_key, None)
        background_tasks = getattr(self, "background_subagent_tasks", {})
        if background_tasks.get(entry.task_id) is entry.handle:
            background_tasks.pop(entry.task_id, None)
            getattr(self, "background_subagent_sessions", {}).pop(
                entry.task_id, None
            )
            getattr(self, "background_subagent_parents", {}).pop(
                entry.task_id, None
            )

        tracker = get_task_tracker()
        tracked = await tracker.get_task(entry.task_id)
        needs_terminal_delivery = bool(
            tracked is not None
            and tracked.status
            not in {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }
        )
        if tracked is None or tracked.status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }:
            return
        await tracker.update_task(
            entry.task_id,
            status=entry.status,
            error=entry.error or None,
            metadata_update={
                "result_preview": str(entry.result or entry.error or "")[:500]
            },
        )
        if needs_terminal_delivery and entry.kind == "inbound_message":
            bus = getattr(self, "bus", None)
            if bus is not None:
                try:
                    await bus.publish_outbound(
                        OutboundMessage(
                            channel=str(entry.metadata.get("channel") or "web"),
                            chat_id=str(entry.metadata.get("chat_id") or "system"),
                            content="",
                            metadata=self._with_trace_metadata(
                                {
                                    "type": "stop_typing",
                                    "task_status": entry.status,
                                    "is_error": entry.status == TaskStatus.FAILED.value,
                                    "is_cancellation": entry.status
                                    == TaskStatus.CANCELLED.value,
                                },
                                task_id=entry.task_id,
                            ),
                        )
                    )
                except Exception:
                    pass

    async def _dispatch_message(self, msg: InboundMessage) -> asyncio.Task:
        """Register an inbound turn before scheduling its coroutine.

        The old dispatch path created an untracked asyncio task.  If shutdown
        or the user's stop action raced before ``_process_message`` assigned
        ``active_tasks[session_key]``, that turn could continue forever from the
        UI's point of view.  Durable and live identities are now allocated at
        the dispatch boundary.
        """
        from core.task_tracker import get_task_tracker

        task_id = uuid.uuid4().hex[:12]
        tracker = get_task_tracker()
        await tracker.create_task(
            task_type="inbound_message",
            summary=f"{msg.channel}: {(msg.content or '')[:80]}",
            channel=msg.channel,
            session_key=msg.session_key,
            chat_id=msg.chat_id,
            metadata={
                "runtime": True,
                "sender_id": msg.sender_id,
                "workspace_id": msg.metadata.get("workspace_id")
                if isinstance(msg.metadata, dict)
                else "",
                "client_message_id": msg.metadata.get("client_message_id")
                if isinstance(msg.metadata, dict)
                else "",
            },
            task_id=task_id,
        )
        start_gate = asyncio.Event()
        handle = asyncio.create_task(
            self._run_managed_message(msg, task_id, start_gate),
            name=f"limebot-turn-{task_id}",
        )
        try:
            await self._get_task_registry().register(
                task_id,
                handle,
                kind="inbound_message",
                session_key=msg.session_key,
                metadata={"channel": msg.channel, "chat_id": msg.chat_id},
                on_terminal=self._persist_managed_task_terminal,
            )
        except BaseException:
            handle.cancel()
            await asyncio.gather(handle, return_exceptions=True)
            raise
        finally:
            start_gate.set()
        # Keep this index for existing channel adapters; the registry is the
        # authoritative source and also retains queued turns before this map is
        # populated by the inner processing path.
        self.active_tasks[msg.session_key] = handle
        return handle

    async def _run_managed_message(
        self,
        msg: InboundMessage,
        task_id: str,
        start_gate: Optional[asyncio.Event] = None,
    ) -> None:
        from core.task_tracker import TaskStatus, get_task_tracker

        tracker = get_task_tracker()
        registry = self._get_task_registry()
        if start_gate is not None:
            await start_gate.wait()
        await registry.mark_running(task_id)
        try:
            await self._process_message(msg, _task_id=task_id)
        except asyncio.CancelledError:
            # The inner processor may not have started far enough to create its
            # normal finalization record, so the wrapper closes that gap.
            with_cancel_error = "Inbound turn cancelled."
            tracked = await tracker.get_task(task_id)
            if tracked is not None and tracked.status not in {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }:
                await tracker.update_task(
                    task_id,
                    status=TaskStatus.CANCELLED.value,
                    error=with_cancel_error,
                )
            raise
        except Exception as exc:
            logger.exception("Unhandled managed inbound turn error: %s", exc)
            tracked = await tracker.get_task(task_id)
            if tracked is not None and tracked.status not in {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }:
                await tracker.update_task(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    error=str(exc)[:500],
                )
        finally:
            tracked = await tracker.get_task(task_id)
            status = (
                tracked.status
                if tracked is not None
                else TaskStatus.COMPLETED.value
            )
            if status not in {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }:
                current = asyncio.current_task()
                status = (
                    TaskStatus.CANCELLED.value
                    if current
                    and getattr(current, "cancelling", lambda: 0)()
                    else TaskStatus.COMPLETED.value
                )
                await tracker.update_task(task_id, status=status)
            await registry.finalize(
                task_id,
                status,
                error=(tracked.error if tracked is not None else None),
            )
            if self.active_tasks.get(msg.session_key) is asyncio.current_task():
                self.active_tasks.pop(msg.session_key, None)

    async def run(self) -> None:
        self._running = True
        logger.info(f"Agent loop started (model: {self.model})")
        while self._running:
            try:
                msg = await self.bus.consume_inbound()
                await self._dispatch_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in agent loop: {e}")

    async def stop(self) -> None:
        self._running = False
        if self._initialization_task and not self._initialization_task.done():
            self._initialization_task.cancel()
            await asyncio.gather(self._initialization_task, return_exceptions=True)

        # A config change restarts the backend.  The managed registry owns both
        # ordinary turns and background subagents, so shutdown can cancel and
        # await them through one lifecycle instead of relying on detached maps.
        current_task = asyncio.current_task()
        await self._get_task_registry().cancel_all(
            exclude={current_task} if current_task is not None else None
        )

        # Keep a compatibility fallback for test doubles or legacy tasks that
        # were inserted directly into the old maps.
        active_task_registry = getattr(self, "active_tasks", {})
        legacy_handles = list(active_task_registry.values()) + list(
            getattr(self, "background_subagent_tasks", {}).values()
        )
        legacy_tasks = [
            task
            for task in legacy_handles
            if task is not current_task and not task.done()
        ]
        for task in legacy_tasks:
            task.cancel()
        if legacy_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*legacy_tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out waiting for %d legacy task handles during shutdown.",
                    len(legacy_tasks),
                )
        active_task_registry.clear()
        getattr(self, "background_subagent_tasks", {}).clear()
        getattr(self, "background_subagent_sessions", {}).clear()
        getattr(self, "background_subagent_parents", {}).clear()

        # Persist the latest durable delivery state before bus workers are cancelled.
        try:
            from core.delivery_tracker import get_delivery_tracker

            await asyncio.wait_for(get_delivery_tracker().flush(), timeout=2.0)
        except Exception:
            pass
        # Metrics persistence is best-effort and must not pin the event loop.
        try:
            await asyncio.to_thread(self.metrics.flush, 2.0)
        except Exception:
            pass

    @staticmethod
    def _task_channel_chat(session_key: str) -> Tuple[str, str]:
        channel, separator, chat_id = str(session_key or "").partition(":")
        return (channel if separator else "", chat_id if separator else session_key)

    async def start_background_subagent(
        self,
        parent_session_key: str,
        sub_session_key: str,
        task: str,
        agent_name: Optional[str] = None,
        isolated_workspace: Any = None,
        isolation_mode: str = "none",
    ) -> str:
        """Create and retain a durable, cancellable background subagent job."""
        from core.task_tracker import TaskStatus, TaskType, get_task_tracker

        tracker = get_task_tracker()
        channel, chat_id = self._task_channel_chat(parent_session_key)
        task_id = await tracker.create_task(
            task_type=TaskType.SUBAGENT_JOB.value,
            summary=str(task or "").strip()[:300],
            channel=channel,
            session_key=sub_session_key,
            chat_id=chat_id,
            metadata={
                "background": True,
                "parent_session_key": parent_session_key,
                "sub_session_key": sub_session_key,
                "agent_name": agent_name or "",
                "isolation": isolation_mode,
            },
        )
        await tracker.update_task(task_id, status=TaskStatus.RUNNING.value)
        start_gate = asyncio.Event()
        handle = asyncio.create_task(
            self._run_background_subagent_task(
                task_id,
                parent_session_key,
                sub_session_key,
                task,
                agent_name,
                start_gate,
                isolated_workspace,
                isolation_mode,
            ),
            name=f"limebot-subagent-{task_id}",
        )
        try:
            await self._get_task_registry().register(
                task_id,
                handle,
                kind=TaskType.SUBAGENT_JOB.value,
                session_key=sub_session_key,
                metadata={
                    "background": True,
                    "parent_session_key": parent_session_key,
                    "agent_name": agent_name or "",
                    "isolation": isolation_mode,
                },
                on_terminal=self._persist_managed_task_terminal,
            )
            await self._get_task_registry().mark_running(task_id)
        except BaseException:
            handle.cancel()
            await asyncio.gather(handle, return_exceptions=True)
            raise
        finally:
            start_gate.set()
        if not hasattr(self, "background_subagent_tasks"):
            self.background_subagent_tasks = {}
        if not hasattr(self, "background_subagent_sessions"):
            self.background_subagent_sessions = {}
        if not hasattr(self, "background_subagent_parents"):
            self.background_subagent_parents = {}
        self.background_subagent_tasks[task_id] = handle
        self.background_subagent_sessions[task_id] = sub_session_key
        self.background_subagent_parents[task_id] = parent_session_key
        if not hasattr(self, "background_subagent_results"):
            self.background_subagent_results = {}
        return task_id

    async def _run_background_subagent_task(
        self,
        task_id: str,
        parent_session_key: str,
        sub_session_key: str,
        task: str,
        agent_name: Optional[str],
        start_gate: Optional[asyncio.Event] = None,
        isolated_workspace: Any = None,
        isolation_mode: str = "none",
    ) -> str:
        from core.task_tracker import TaskStatus, get_task_tracker

        tracker = get_task_tracker()
        if start_gate is not None:
            await start_gate.wait()
        task_context_token = _CURRENT_TASK_ID.set(task_id)
        status = TaskStatus.COMPLETED.value
        error = ""
        result = ""
        try:
            result = str(
                await self.run_subagent(
                    parent_session_key,
                    sub_session_key,
                    task,
                    agent_name=agent_name,
                    isolated_workspace=isolated_workspace,
                    isolation_mode=isolation_mode,
                )
                or ""
            )
            if result.startswith("Error"):
                status = TaskStatus.FAILED.value
                error = result[:500]
            return result
        except asyncio.CancelledError:
            status = TaskStatus.CANCELLED.value
            error = "Background subagent cancelled."
            raise
        except Exception as exc:
            status = TaskStatus.FAILED.value
            error = str(exc)[:500]
            logger.error(f"Error in background sub-agent '{sub_session_key}': {exc}")
            return f"Error in sub-agent '{sub_session_key}': {exc}"
        finally:
            # The wrapper is the only completion owner.  A repeated kill/wait or
            # a late cancellation therefore cannot publish duplicate terminal
            # task records.
            try:
                await asyncio.shield(
                    tracker.update_task(
                        task_id,
                        status=status,
                        summary=(result or error or task).strip()[:300],
                        error=error or None,
                        metadata_update={
                            "outcome": status,
                            "result_preview": (result or error).strip()[:500],
                        },
                    )
                )
            except Exception as exc:
                logger.warning(
                    f"Could not finalize background task {task_id}: {exc}"
                )
            await self._get_task_registry().finalize(
                task_id,
                status,
                result=result or None,
                error=error or None,
                metadata_update={"result_preview": (result or error).strip()[:500]},
            )
            current = asyncio.current_task()
            if result or error:
                self.background_subagent_results[task_id] = result or error
                # Keep completed output bounded while TaskTracker retains the
                # durable status and a short preview.
                while len(self.background_subagent_results) > 500:
                    self.background_subagent_results.pop(
                        next(iter(self.background_subagent_results)), None
                    )
            if self.background_subagent_tasks.get(task_id) is current:
                self.background_subagent_tasks.pop(task_id, None)
            self.background_subagent_sessions.pop(task_id, None)
            self.background_subagent_parents.pop(task_id, None)
            if isolated_workspace is not None:
                try:
                    await isolated_workspace.cleanup()
                except Exception as exc:
                    logger.warning(
                        "Could not clean up isolated workspace for background task %s: %s",
                        task_id,
                        exc,
                    )
            _CURRENT_TASK_ID.reset(task_context_token)

    async def get_background_subagent_task(self, task_id: str):
        from core.task_tracker import get_task_tracker

        return await get_task_tracker().get_task(task_id)

    async def get_background_subagent_output(
        self, task_id: str, timeout: Optional[float] = None
    ) -> Optional[str]:
        if timeout is not None and float(timeout) > 0:
            task = await self.wait_background_subagent_task(task_id, timeout)
        else:
            task = await self.get_background_subagent_task(task_id)
        if task is None:
            return None
        output = getattr(self, "background_subagent_results", {}).get(task_id)
        if output is None:
            output = str((task.metadata or {}).get("result_preview") or "")
        return (
            f"Task {task.task_id}\n"
            f"Status: {task.status}\n"
            f"Summary: {task.summary}\n"
            f"{output}".strip()
        )

    async def wait_background_subagent_task(
        self, task_id: str, timeout: Optional[float] = None
    ):
        managed = await self._get_task_registry().get(task_id)
        if managed is not None and not managed.terminal:
            await self._get_task_registry().wait(task_id, timeout)
        else:
            handle = self.background_subagent_tasks.get(task_id)
            if handle is not None and not handle.done():
                waiter = asyncio.shield(handle)
                if timeout is None:
                    await waiter
                else:
                    await asyncio.wait_for(
                        waiter, timeout=max(0.0, float(timeout))
                    )
        return await self.get_background_subagent_task(task_id)

    async def kill_background_subagent_task(self, task_id: str):
        from core.task_tracker import TaskStatus, TaskType, get_task_tracker

        tracker = get_task_tracker()
        managed = await self._get_task_registry().get(task_id)
        if managed is not None and not managed.terminal:
            await self._get_task_registry().cancel(task_id)
        else:
            handle = self.background_subagent_tasks.get(task_id)
            if handle is not None and not handle.done():
                handle.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(handle, return_exceptions=True),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out waiting for background task %s to cancel.",
                        task_id,
                    )
        task = await tracker.get_task(task_id)
        if (
            task is not None
            and task.type == TaskType.SUBAGENT_JOB.value
            and task.status not in {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }
        ):
            task = await tracker.cancel_task(task_id)
        return task

    # Short lifecycle aliases mirror Grok's task vocabulary for callers that
    # manage jobs directly instead of going through the REST/tool adapters.
    async def get_task(self, task_id: str):
        return await self.get_background_subagent_task(task_id)

    async def wait_task(self, task_id: str, timeout: Optional[float] = None):
        return await self.wait_background_subagent_task(task_id, timeout)

    async def kill_task(self, task_id: str):
        return await self.kill_background_subagent_task(task_id)

    async def cancel_session(self, session_key: str) -> bool:
        cancelled_any = False
        self._log_session_event(
            session_key,
            {"type": "cancel_requested", "session_key": session_key},
        )

        related_sessions = {session_key}
        for sk, meta in list(getattr(self.session_manager, "sessions", {}).items()):
            if meta.get("parent_id") == session_key:
                related_sessions.add(sk)

        managed_entries = await self._get_task_registry().active_for(
            lambda entry: (
                entry.session_key in related_sessions
                or str(entry.metadata.get("parent_session_key") or "")
                in related_sessions
            )
        )
        for entry in managed_entries:
            if await self._get_task_registry().cancel(entry.task_id):
                cancelled_any = True

        for sk in related_sessions:
            task = self.active_tasks.get(sk)
            if task and not task.done():
                task.cancel()
                logger.info(f"🛑 Cancelled task for {sk}")
                self._log_session_event(
                    sk,
                    {
                        "type": "task_cancelled",
                        "session_key": sk,
                        "parent": session_key,
                    },
                )
                cancelled_any = True

        # A background subagent is keyed by its stable task id rather than by
        # the parent session's short-lived turn handle.
        for task_id, sub_session in list(
            getattr(self, "background_subagent_sessions", {}).items()
        ):
            parent = getattr(self, "background_subagent_parents", {}).get(task_id)
            if parent not in related_sessions and sub_session not in related_sessions:
                continue
            if await self.kill_background_subagent_task(task_id):
                logger.info(f"Cancelled background subagent task {task_id}")
                cancelled_any = True

        for conf_id, conf in list(self.pending_confirmations.items()):
            if conf.get("session_key") not in related_sessions:
                continue
            conf["approved"] = False
            event = conf.get("event")
            if event:
                event.set()
            logger.info(
                f"🛑 Released pending confirmation {conf_id} for {conf.get('session_key')}"
            )
            self._log_session_event(
                conf.get("session_key") or session_key,
                {
                    "type": "confirmation_released",
                    "confirmation_id": conf_id,
                    "tool": conf.get("tool"),
                },
            )
            cancelled_any = True

        for sk, t in list(self.active_tasks.items()):
            if t.done():
                del self.active_tasks[sk]

        return cancelled_any

    async def confirm_tool(
        self,
        conf_id: str,
        approved: bool,
        session_whitelist: bool = False,
        source: str = "api",
    ) -> bool:
        if conf_id in self.pending_confirmations:
            conf = self.pending_confirmations[conf_id]
            source = str(source or "api").strip().lower()
            if source not in {
                "api",
                "app",
                "web",
                "discord",
                "extension",
                "whatsapp",
            }:
                source = "api"
            profile = str(conf.get("policy_profile") or "manual")
            effective_whitelist = bool(
                approved
                and session_whitelist
                and profile in {"manual", "session"}
            )
            self._log_session_event(
                conf.get("session_key", "unknown"),
                {
                    "type": "approval_decided",
                    "conf_id": conf_id,
                    "approved": approved,
                    "tool": conf.get("tool"),
                    "session_whitelist": effective_whitelist,
                    "policy_profile": profile,
                    "decision_reason": "user_approved" if approved else "user_denied",
                    "client_source": source,
                },
            )
            conf["approved"] = approved
            if effective_whitelist:
                sk = conf["session_key"]
                whitelist_key = conf.get("whitelist_key") or conf["tool"]
                self.session_whitelists.setdefault(sk, set()).add(whitelist_key)
                logger.info(f"🔓 Added {whitelist_key} to whitelist for {sk}")
            conf["event"].set()
            logger.info(f"✅ Tool {conf_id} {'approved' if approved else 'denied'}")
            return True
        logger.warning(f"⚠️ Confirmation {conf_id} not found or expired.")
        return False

    def _mark_dirty(self, session_key: str) -> None:
        self._history_dirty[session_key] = True

    async def _flush_history(self, session_key: str, force: bool = False) -> None:
        """Persist history only if it changed since the last flush."""
        if not (self._history_dirty.get(session_key) and session_key in self.history):
            return

        now = time.monotonic()
        last = self._last_history_flush.get(session_key, 0.0)
        if not force and (now - last) < self._history_flush_interval:
            return

        persistable_history = []
        for message in self.history[session_key]:
            persisted_message = dict(message)
            persisted_message["content"] = self._image_safe_history_content(
                message.get("content")
            )
            persistable_history.append(persisted_message)
        await self.session_manager.save_history(session_key, persistable_history)
        self._history_dirty[session_key] = False
        self._last_history_flush[session_key] = now

    async def run_subagent(
        self,
        parent_session_key: str,
        sub_session_key: str,
        task: str,
        agent_name: Optional[str] = None,
        isolated_workspace: Any = None,
        isolation_mode: str = "none",
    ) -> str:
        workspace_token = None
        try:
            if isolated_workspace is not None:
                workspace_token = isolated_workspace.activate()
            logger.info(f"[SUB-AGENT] {sub_session_key} ← {parent_session_key}: {task}")

            subagent_profile = self.subagent_registry.get_subagent(agent_name)
            if agent_name and not subagent_profile:
                return f"Error: Unknown subagent '{agent_name}'"

            subagent_model = (
                (subagent_profile or {}).get("model") or "inherit"
            ).strip()
            subagent_max_turns = (subagent_profile or {}).get("max_turns") or 10
            allowed_tools = None
            tool_definitions_override = None
            disallowed_tools = {
                normalize_subagent_tool_name(name)
                for name in ((subagent_profile or {}).get("disallowed_tools") or [])
                if normalize_subagent_tool_name(name)
            }
            if subagent_profile:
                tool_definitions_override = self._filter_tool_definitions_for_subagent(
                    subagent_profile.get("tools"),
                    subagent_profile.get("disallowed_tools"),
                )
                if subagent_profile.get("tools") is not None:
                    allowed_tools = {
                        tool.get("function", {}).get("name")
                        for tool in tool_definitions_override
                        if tool.get("function", {}).get("name")
                    }

            session_model = (
                self.model
                if not subagent_model or subagent_model == "inherit"
                else subagent_model
            )
            await self.session_manager.update_session(
                session_key=sub_session_key,
                model=session_model,
                origin=f"subagent:{parent_session_key}",
                parent_id=parent_session_key,
                task=task,
                subagent_name=agent_name or "",
            )

            try:
                soul = SOUL_FILE.read_text(encoding="utf-8")
                identity = IDENTITY_FILE.read_text(encoding="utf-8")
            except Exception:
                soul = "You are a helpful assistant."
                identity = "Name: LimeBot Sub-Agent"

            profile_lines = []
            if subagent_profile:
                description = (subagent_profile.get("description") or "").strip()
                prompt = (subagent_profile.get("prompt") or "").strip()
                if description:
                    profile_lines.append(f"Profile: {description}")
                if prompt:
                    profile_lines.append(prompt)
                if allowed_tools is None:
                    profile_lines.append("Tool access: inherit the main toolset.")
                else:
                    listed_tools = ", ".join(sorted(allowed_tools)) or "none"
                    profile_lines.append(
                        f"Tool access: only use these tools: {listed_tools}."
                    )
                if disallowed_tools:
                    profile_lines.append(
                        "Never use these tools: "
                        + ", ".join(sorted(disallowed_tools))
                        + "."
                    )
                if subagent_profile.get("background"):
                    profile_lines.append(
                        "This profile is intended for background-friendly work when delegated asynchronously."
                    )
                if subagent_max_turns:
                    profile_lines.append(
                        f"Complete the task within at most {subagent_max_turns} assistant turns."
                    )

            profile_block = "\n".join(line for line in profile_lines if line).strip()
            if profile_block:
                profile_block += "\n"

            workspace_instructions = ""
            if isolated_workspace is not None:
                workspace_instructions = (
                    "Workspace isolation: You are working in a temporary copy of the project. "
                    "Use relative paths from the workspace root; do not use parent-directory paths "
                    "or absolute paths into the live project. Changes are not merged automatically. "
                    "At the end, report changed files and verification results.\n"
                )

            sub_system = (
                f"{soul}\n\n{identity}\n\n"
                "--- SUB-AGENT INSTRUCTIONS ---\n"
                + (
                    f"You are the '{agent_name}' subagent.\n"
                    if agent_name and subagent_profile
                    else "You are a generic sub-agent.\n"
                )
                + profile_block
                + workspace_instructions
                + f"Primary task: {task}\n"
                + "Work independently, use tools when needed, and return a concise final result.\n" +
                "DO NOT start a conversation — JUST COMPLETE THE TASK.\n"
            )

            sub_history: List[Dict] = [
                {"role": "system", "content": sub_system},
                {"role": "user", "content": f"Task: {task}"},
            ]
            asyncio.create_task(
                self.session_manager.append_chat_log(
                    sub_session_key, {"role": "user", "content": task}
                )
            )

            iteration = 0
            final_result = ""

            while iteration < subagent_max_turns:
                iteration += 1
                logger.info(f"[SUB-AGENT:{sub_session_key}] iteration {iteration}")

                response = await self._llm_call_with_retry(
                    messages=sub_history,
                    session_key=sub_session_key,
                    msg=None,
                    stream=False,
                    tool_context_text=task,
                    tool_definitions_override=tool_definitions_override,
                    model_override=subagent_model,
                )

                if hasattr(response, "usage"):
                    await self.session_manager.update_session(
                        session_key=sub_session_key,
                        model=session_model,
                        origin=f"subagent:{parent_session_key}",
                        usage=response.usage,
                    )

                assistant_msg = response.choices[0].message
                full_content = assistant_msg.content or ""
                tool_calls_raw = assistant_msg.tool_calls

                sub_history.append(assistant_msg.model_dump())
                asyncio.create_task(
                    self.session_manager.append_chat_log(
                        sub_session_key, {"role": "assistant", "content": full_content}
                    )
                )

                if not tool_calls_raw:
                    final_result = full_content
                    break

                for tc in tool_calls_raw:
                    tc_id = tc.id
                    fn = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                        fn, args = self._normalize_tool_alias(
                            fn, args, sub_session_key
                        )
                        logger.info(
                            f"[SUB-AGENT:{sub_session_key}] "
                            f"{fn}({redact_sensitive_text(args)})"
                        )
                        if (allowed_tools is not None and fn not in allowed_tools) or (
                            fn in disallowed_tools
                        ):
                            result = (
                                f"Error: Tool '{fn}' is not allowed for subagent "
                                f"'{agent_name}'."
                            )
                        else:
                            result = await self._execute_tool(
                                fn, args, sub_session_key
                            )
                    except Exception as e:
                        result = f"Error: {e}"

                    sub_history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": fn,
                            "content": str(result),
                        }
                    )

            if not str(final_result or "").strip():
                summary_prompt = (
                    "Final response only. Do not call any more tools. "
                    "Do not mention truncation, token limits, tool budgets, or that you are an assistant. "
                    "Use only the work already completed. "
                    "If this was a review or verification task, return a concise report with findings or outcome, "
                    "a short rating if appropriate, and 2-4 concrete suggestions. "
                    "If anything remains unverified, state that plainly."
                )
                sub_history.append({"role": "system", "content": summary_prompt})
                try:
                    summary_response = await self._llm_call_with_retry(
                        messages=sub_history,
                        session_key=sub_session_key,
                        msg=None,
                        stream=False,
                        include_tools=False,
                        tool_context_text=task,
                        model_override=subagent_model,
                    )
                    summary_msg = summary_response.choices[0].message
                    final_result = (
                        self._clean_subagent_final_result(summary_msg.content or "")
                        or f"Stopped after reaching max_turns ({subagent_max_turns}) without a final answer."
                    )
                    sub_history.append(summary_msg.model_dump())
                    asyncio.create_task(
                        self.session_manager.append_chat_log(
                            sub_session_key,
                            {"role": "assistant", "content": final_result},
                        )
                    )
                except Exception as summary_error:
                    logger.warning(
                        f"[SUB-AGENT:{sub_session_key}] failed to produce final summary after max_turns: {summary_error}"
                    )
                    final_result = (
                        f"Stopped after reaching max_turns ({subagent_max_turns}) without a final answer."
                    )

            final_result = self._clean_subagent_final_result(final_result)

            report_title = (
                f"--- SUB-AGENT REPORT ({sub_session_key}) [{agent_name}] ---"
                if agent_name
                else f"--- SUB-AGENT REPORT ({sub_session_key}) ---"
            )
            report = (
                f"{report_title}\n"
                f"Task: {task}\n"
                f"Result:\n{final_result or '(Silently completed)'}\n"
            )

            workspace_capture = None
            workspace_report = None
            if isolated_workspace is not None:
                try:
                    workspace_capture = await isolated_workspace.capture()
                    workspace_report = isolated_workspace.report_metadata(
                        workspace_capture
                    )
                    diff_entries = []
                    diff_budget = _SUBAGENT_REPORT_DIFF_MAX_CHARS
                    for changed_file in workspace_capture.get("changed_files", []):
                        diff = str(changed_file.get("diff") or "")
                        if not diff or diff_budget <= 0:
                            continue
                        clipped_diff = diff[:diff_budget]
                        diff_entries.append(
                            {
                                "path": changed_file.get("path", ""),
                                "status": changed_file.get("status", "modified"),
                                "diff": clipped_diff,
                            }
                        )
                        diff_budget -= len(clipped_diff)
                    if diff_entries:
                        workspace_report["diff"] = diff_entries
                    report += (
                        "Workspace changes (not merged):\n"
                        + json.dumps(workspace_report, ensure_ascii=False)
                        + "\n"
                    )
                except Exception as workspace_error:
                    logger.warning(
                        f"[SUB-AGENT:{sub_session_key}] failed to capture workspace changes: "
                        f"{workspace_error}"
                    )

            parts = parent_session_key.split(":", 1)
            if len(parts) == 2:
                report_metadata = {
                    "is_report": True,
                    "subagent_id": sub_session_key,
                    "isolation": isolation_mode,
                }
                if workspace_capture is not None:
                    report_metadata["workspace"] = workspace_capture
                await self.bus.publish_inbound(
                    InboundMessage(
                        channel=parts[0],
                        sender_id="system",
                        chat_id=parts[1],
                        content=report,
                        metadata=report_metadata,
                    )
                )
            return report

        except Exception as e:
            logger.error(f"Error in sub-agent '{sub_session_key}': {e}")
            return f"Error in sub-agent '{sub_session_key}': {e}"
        finally:
            if workspace_token is not None:
                try:
                    isolated_workspace.deactivate(workspace_token)
                except Exception as exc:
                    logger.warning(
                        f"Could not restore workspace context for sub-agent '{sub_session_key}': {exc}"
                    )
            if isolated_workspace is not None:
                try:
                    await isolated_workspace.cleanup()
                except Exception as exc:
                    logger.warning(
                        f"Could not clean up isolated workspace for sub-agent '{sub_session_key}': {exc}"
                    )

    @staticmethod
    def _should_include_tools(content: str) -> bool:
        """
        Keep tool routing simple and predictable: non-empty user turns get tools.
        """
        return bool(str(content or "").strip())

    async def _llm_call_with_retry(
        self,
        messages: List[Dict],
        session_key: str,
        msg: Optional[InboundMessage],
        max_retries: int = 3,
        stream: bool = False,
        include_tools: bool = True,
        tool_context_text: str = "",
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
        tool_definitions_override: Optional[List[Dict[str, Any]]] = None,
        model_override: Optional[str] = None,
        tool_choice: Optional[str] = "auto",
    ) -> Any:

        messages = self._sanitize_messages_for_llm(messages, session_key)
        if not include_tools:
            tools = []
        elif tool_definitions_override is not None:
            tools = tool_definitions_override
        else:
            tools = self._get_tool_definitions_for_turn(tool_context_text)
        if model_override and model_override != "inherit":
            override_provider = self.llm_client.resolve_provider(
                model_override,
                default_base_url=self.config.llm.base_url,
            )
            provider_chain = [
                (
                    override_provider.source_model,
                    override_provider.model,
                    override_provider.base_url,
                    override_provider.api_key,
                    override_provider.custom_llm_provider,
                )
            ]
        else:
            provider_chain = self._resolve_provider_chain()
        if not provider_chain:
            raise RuntimeError("No LLM provider is configured")

        breaker = self._get_provider_circuit_breaker()

        def _select_provider(start_index: int):
            for index in range(start_index, len(provider_chain)):
                candidate = provider_chain[index]
                circuit_key, credential_fingerprint = self._provider_circuit_identity(
                    *candidate
                )
                if breaker.allow(
                    circuit_key,
                    credential_fingerprint=credential_fingerprint,
                ):
                    return index, candidate, circuit_key, credential_fingerprint
            return None

        selection = _select_provider(0)
        if selection is None:
            raise ProviderCircuitOpenError("all configured providers")
        selected_index, candidate, circuit_key, credential_fingerprint = selection
        (
            active_source_model,
            model,
            base_url,
            api_key,
            custom_llm_provider,
        ) = candidate
        self._log_tool_debug(
            "llm_call_prepare",
            session_key=session_key,
            model=active_source_model,
            stream=stream,
            include_tools=include_tools,
            tool_context=tool_context_text,
            message_count=len(messages),
            tool_count=len(tools),
            tool_names=self._tool_definition_names(tools),
            last_message=messages[-1].get("content", "") if messages else "",
            fallback_chain=[item[0] for item in provider_chain],
            tool_choice=tool_choice,
        )
        qwen_auth_failover_attempted: Set[str] = set()
        image_fallback_attempted = False
        model_failover_announced = False

        for attempt in range(max_retries):
            try:
                provider = self._build_provider_config(
                    active_source_model,
                    model,
                    base_url,
                    api_key,
                    custom_llm_provider,
                )
                llm_timeout = float(getattr(self.config, "command_timeout", 300.0) or 0)
                llm_call = self.llm_client.complete(
                    provider,
                    ChatRequest(
                        messages=messages,
                        tools=tools or None,
                        stream=stream,
                        session_id=session_key,
                        tool_choice=tool_choice,
                    ),
                )
                if llm_timeout > 0:
                    response = await asyncio.wait_for(llm_call, timeout=llm_timeout)
                else:
                    response = await llm_call
                breaker.record_success(
                    circuit_key,
                    credential_fingerprint=credential_fingerprint,
                )
                return response
            except AuthenticationError as e:
                failed_source_model = active_source_model
                breaker.record_failure(
                    circuit_key,
                    credential_fingerprint=credential_fingerprint,
                    authentication=True,
                )
                self._log_tool_debug(
                    "llm_call_error",
                    session_key=session_key,
                    attempt=attempt + 1,
                    source_model=active_source_model,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                selection = _select_provider(selected_index + 1)
                if selection:
                    selected_index, candidate, circuit_key, credential_fingerprint = selection
                    (
                        active_source_model,
                        model,
                        base_url,
                        api_key,
                        custom_llm_provider,
                    ) = candidate
                    qwen_auth_failover_attempted.clear()
                    logger.warning(
                        f"LLM auth failed for '{failed_source_model}'; switching to fallback '{active_source_model}'."
                    )
                    if msg and not model_failover_announced:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                content=f"Switching AI provider to fallback model `{active_source_model}`...",
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                metadata=self._with_trace_metadata(
                                    {"reply_to": msg.sender_id, "is_warning": True},
                                    turn_id=turn_id,
                                    message_id=message_id,
                                ),
                            )
                        )
                        model_failover_announced = True
                    continue
                # DashScope keys can be region-scoped. If auth fails on one endpoint,
                # try other official compatible endpoints before failing.
                current_base = (base_url or "").lower()
                is_qwen = (active_source_model or "").startswith(
                    "qwen/"
                ) or "dashscope" in current_base
                if is_qwen:
                    qwen_auth_failover_attempted.add(current_base)
                    fallback_bases = [
                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                        "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    ]
                    next_base = next(
                        (
                            url
                            for url in fallback_bases
                            if url.lower() not in qwen_auth_failover_attempted
                        ),
                        None,
                    )
                    if next_base:
                        base_url = next_base
                        logger.warning(
                            f"Qwen auth failed; retrying with alternate endpoint: {base_url}"
                        )
                        continue
                raise

            except (
                RateLimitError,
                InternalServerError,
                APIConnectionError,
                ServiceUnavailableError,
            ) as e:
                failed_source_model = active_source_model
                breaker.record_failure(
                    circuit_key,
                    credential_fingerprint=credential_fingerprint,
                )
                self._log_tool_debug(
                    "llm_call_error",
                    session_key=session_key,
                    attempt=attempt + 1,
                    source_model=active_source_model,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                is_500 = isinstance(e, InternalServerError)
                is_conn = isinstance(e, (APIConnectionError, ServiceUnavailableError))
                wait_time = (2**attempt) * 5
                selection = _select_provider(selected_index + 1)
                if selection:
                    selected_index, candidate, circuit_key, credential_fingerprint = selection
                    (
                        active_source_model,
                        model,
                        base_url,
                        api_key,
                        custom_llm_provider,
                    ) = candidate
                    qwen_auth_failover_attempted.clear()
                    logger.warning(
                        f"LLM provider '{failed_source_model}' failed; switching to fallback '{active_source_model}'."
                    )
                    if msg and not model_failover_announced:
                        err_str = str(e).lower()
                        if (
                            "rate limit" in err_str
                            or "rate_limit" in err_str
                            or "usage_limit" in err_str
                            or "usage limit" in err_str
                            or "limit has been reached" in err_str
                            or "limit_reached" in err_str
                            or "codex provider returned an error" in err_str
                            or "no visible response" in err_str
                            or isinstance(e, RateLimitError)
                            or "RateLimit" in type(e).__name__
                        ):
                            reason = "rate limit or usage limit"
                        elif is_conn:
                            reason = "connection error"
                        elif is_500:
                            reason = "server error"
                        else:
                            reason = "temporary error"
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                content=f"Primary AI model hit a {reason}, switching to fallback model `{active_source_model}`...",
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                metadata=self._with_trace_metadata(
                                    {"reply_to": msg.sender_id, "is_warning": True},
                                    turn_id=turn_id,
                                    message_id=message_id,
                                ),
                            )
                        )
                        model_failover_announced = True
                    continue
                if is_conn:
                    error_type = "Connection error"
                elif is_500:
                    error_type = "Server error (500)"
                else:
                    error_type = "Rate limit"
                logger.warning(
                    f"⚠ {error_type} attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s…"
                )

                if attempt == 0 and msg:
                    if is_conn:
                        text = "⏳ Connection lost — retrying when network is back…"
                    elif is_500:
                        text = "⏳ AI service unstable — retrying…"
                    else:
                        text = "⏳ Rate limit hit — retrying…"
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            content=text,
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            metadata=self._with_trace_metadata(
                                {"reply_to": msg.sender_id, "is_warning": True},
                                turn_id=turn_id,
                                message_id=message_id,
                            ),
                        )
                    )

                await asyncio.sleep(wait_time)

                if attempt == max_retries - 1:
                    if is_conn:
                        content = "❌ Cannot reach AI service. Check your internet connection."
                    elif is_500:
                        content = "❌ AI service experiencing errors. Try again in a few minutes."
                    else:
                        content = "❌ API rate limit exceeded. Please wait a minute."
                    if msg:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                content=content,
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                metadata=self._with_trace_metadata(
                                    {"reply_to": msg.sender_id, "is_error": True},
                                    turn_id=turn_id,
                                    message_id=message_id,
                                ),
                            )
                        )
                raise
            except asyncio.CancelledError:
                breaker.record_aborted(
                    circuit_key,
                    credential_fingerprint=credential_fingerprint,
                )
                raise
            except asyncio.TimeoutError as e:
                breaker.record_failure(
                    circuit_key,
                    credential_fingerprint=credential_fingerprint,
                )
                self._log_tool_debug(
                    "llm_call_error",
                    session_key=session_key,
                    attempt=attempt + 1,
                    source_model=active_source_model,
                    error_type=type(e).__name__,
                    error=f"Timed out after {llm_timeout:g}s",
                )
                raise TimeoutError(
                    f"AI provider timed out after {llm_timeout:g}s. Try again or switch models."
                ) from e
            except Exception as e:
                failed_source_model = active_source_model
                auth_like_error = any(
                    marker in str(e or "").lower()
                    for marker in ("incorrect api key", "invalid api key", "authentication", "auth failed")
                )
                if auth_like_error:
                    breaker.record_failure(
                        circuit_key,
                        credential_fingerprint=credential_fingerprint,
                        authentication=True,
                    )
                elif self._should_failover_model(e):
                    breaker.record_failure(
                        circuit_key,
                        credential_fingerprint=credential_fingerprint,
                    )
                self._log_tool_debug(
                    "llm_call_error",
                    session_key=session_key,
                    attempt=attempt + 1,
                    source_model=active_source_model,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                failover_worthy = self._should_failover_model(e)
                selection = (
                    _select_provider(selected_index + 1)
                    if failover_worthy
                    else None
                )
                if selection and failover_worthy:
                    selected_index, candidate, circuit_key, credential_fingerprint = selection
                    (
                        active_source_model,
                        model,
                        base_url,
                        api_key,
                        custom_llm_provider,
                    ) = candidate
                    qwen_auth_failover_attempted.clear()
                    logger.warning(
                        f"LLM call failed for '{failed_source_model}'; switching to fallback '{active_source_model}'."
                    )
                    if msg and not model_failover_announced:
                        err_msg = str(e).lower()
                        if (
                            "rate limit" in err_msg
                            or "rate_limit" in err_msg
                            or "usage_limit" in err_msg
                            or "usage limit" in err_msg
                            or "limit has been reached" in err_msg
                            or "limit_reached" in err_msg
                            or "codex provider returned an error" in err_msg
                            or "no visible response" in err_msg
                            or "429" in err_msg
                        ):
                            reason_phrase = "rate limit or usage limit being reached"
                        elif "auth" in err_msg or "api key" in err_msg or "authentication" in err_msg:
                            reason_phrase = "authentication failure"
                        else:
                            reason_phrase = "an error"
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                content=f"Primary AI model failed due to {reason_phrase}, switching to fallback model `{active_source_model}`...",
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                metadata=self._with_trace_metadata(
                                    {"reply_to": msg.sender_id, "is_warning": True},
                                    turn_id=turn_id,
                                    message_id=message_id,
                                ),
                            )
                        )
                        model_failover_announced = True
                    continue
                if (
                    not image_fallback_attempted
                    and self._should_retry_without_images(e, messages)
                    and self._downgrade_image_messages_for_text_model(
                        messages, session_key
                    )
                ):
                    image_fallback_attempted = True
                    self._disable_image_inputs_for_session(session_key)
                    logger.warning(
                        f"Model '{self.model}' rejected image input for {session_key}; retrying without multimodal content."
                    )
                    continue
                raise

    async def _trim_history(self, session_key: str, max_tokens: int = 12_000) -> None:
        if session_key not in self.history or len(self.history[session_key]) <= 1:
            return

        # Normalize before measuring so an interrupted or oversized tool
        # result cannot consume the entire next model context window.
        self._normalize_history_in_place(session_key)
        history = self.history[session_key]
        system_msg = history[0]
        conv = history[1:]

        for m in conv[:-2]:
            c = m.get("content")
            if isinstance(c, list) and any(
                isinstance(x, dict) and x.get("type") == "image_url" for x in c
            ):
                text = " ".join(
                    x.get("text", "")
                    for x in c
                    if isinstance(x, dict) and x.get("type") == "text"
                )
                m["content"] = text + " [Image evicted]"

        for m in conv[:-2]:
            if m.get("role") == "tool":
                c = str(m.get("content", ""))
                if len(c) > 200 and "[SQUASHED" not in c:
                    name = m.get("name", "tool")
                    m["content"] = (
                        f"[SQUASHED - {name}]\n{c[:80]}…\n…({len(c)} chars)…\n…{c[-80:]}"
                    )

        sys_tokens = self._estimate_tokens([system_msg])
        total_tokens = self._estimate_tokens(conv) + sys_tokens

        if total_tokens <= max_tokens:
            self.history[session_key] = [system_msg] + conv
            self._normalize_history_in_place(session_key)
            return

        logger.info(f"🔄 Token limit ({max_tokens}) reached. Summarising…")

        latest_user_index = max(
            (
                index
                for index, message in enumerate(conv)
                if message.get("role") == "user"
            ),
            default=len(conv),
        )
        if latest_user_index <= 0:
            logger.warning(
                "Current user turn exceeds the history budget; preserving it for the model."
            )
            self.history[session_key] = [system_msg] + conv
            self._normalize_history_in_place(session_key)
            self._mark_dirty(session_key)
            return

        num_to_summarise = min(max(1, len(conv) // 3), latest_user_index)
        to_summarise = conv[:num_to_summarise]
        remaining = conv[num_to_summarise:]

        while remaining and remaining[0].get("role") == "tool":
            to_summarise.append(remaining.pop(0))

        try:
            summary_messages = [
                {
                    "role": "system",
                    "content": "Summarise in ≤200 words: key decisions, user facts, task state.",
                },
                {
                    "role": "user",
                    "content": self._history_summary_payload(to_summarise),
                },
            ]
            provider = self.llm_client.resolve_provider(
                self.model,
                default_base_url=self.config.llm.base_url,
            )
            resp = await self.llm_client.complete(
                provider,
                ChatRequest(
                    messages=summary_messages,
                    max_tokens=300,
                    session_id=f"{session_key}::history-summary",
                ),
            )
            summary_text = resp.choices[0].message.content
            summary_msg = {
                "role": "system",
                "content": f"--- CONTEXT SUMMARY ---\n{summary_text}\n--- END ---",
            }
            idx = next(
                (
                    i
                    for i, m in enumerate(remaining)
                    if m.get("role") == "system"
                    and "CONTEXT SUMMARY" in m.get("content", "")
                ),
                -1,
            )
            if idx != -1:
                remaining[idx] = summary_msg
            else:
                remaining.insert(0, summary_msg)
            conv = remaining
            logger.info(f"✅ Summarised {num_to_summarise} messages.")

        except Exception as e:
            logger.error(f"❌ Summarisation failed: {e}. Falling back to truncation.")

            target_tokens = max_tokens - sys_tokens
            conv = self._truncate_history_fallback(conv, target_tokens)

        self.history[session_key] = [system_msg] + conv
        self._normalize_history_in_place(session_key)
        self._mark_dirty(session_key)

    async def _execute_browser_tool(
        self, function_name: str, args: Dict[str, Any], session_key: str
    ) -> Any:
        try:
            browser = await get_browser_manager(
                session_key=session_key, config=self.config
            )
            on_progress = self.toolbox.send_progress

            dispatch: Dict[str, Any] = {
                "browser_navigate": lambda: browser.navigate(
                    args.get("url", ""), on_progress=on_progress
                ),
                "browser_click": lambda: browser.click(args.get("element_id", "")),
                "browser_download": lambda: browser.download(
                    args.get("element_id", ""),
                    args.get("filename", ""),
                    args.get("timeout_ms", 30_000),
                ),
                "browser_type": lambda: browser.type_text(
                    args.get("element_id", ""), args.get("text", "")
                ),
                "browser_snapshot": lambda: browser.snapshot(),
                "browser_scroll": lambda: browser.scroll(
                    args.get("direction", "down"), args.get("amount", 500)
                ),
                "browser_wait": lambda: browser.wait(args.get("ms", 1000)),
                "browser_press_key": lambda: browser.press_key(
                    args.get("key", "Enter")
                ),
                "browser_go_back": lambda: browser.go_back(),
                "browser_tabs": lambda: browser.list_tabs(),
                "browser_switch_tab": lambda: browser.switch_tab(args.get("index", 0)),
                "browser_extract": lambda: browser.extract(
                    args.get("selector", "body"), limit=args.get("limit", 5000)
                ),
                "browser_extract_large": lambda: browser.extract(
                    args.get("selector", "body"), limit=100_000
                ),
                "browser_get_page_text": lambda: browser.get_page_text(),
                "browser_list_media": lambda: browser.list_media(
                    on_progress=on_progress
                ),
                "google_search": lambda: browser.google_search(
                    args.get("query", ""), on_progress=on_progress
                ),
            }

            handler = dispatch.get(function_name)
            if handler is None:
                return f"Error: Unknown browser tool '{function_name}'"

            result = await handler()

            if isinstance(result, dict):
                if result.get("success"):
                    parts = []
                    for key, label in [
                        ("query", "**Search Query:**"),
                        ("results_summary", "**Results:**"),
                        ("title", "**Page:**"),
                        ("url", "**URL:**"),
                        ("note", "**Note:**"),
                        ("warning", "**Warning:**"),
                        ("message", None),
                        ("elements", "**Elements:**"),
                        ("media_summary", None),
                    ]:
                        val = result.get(key)
                        if val:
                            parts.append(f"{label}\n{val}" if label else val)
                    if result.get("text"):
                        parts.append(f"**Content:**\n{result['text'][:2000]}")
                    return "\n".join(parts) if parts else "Success"
                return f"Error: {result.get('error', 'Unknown error')}"

            if isinstance(result, list):
                return "Open Tabs:\n" + "".join(
                    f"{t['index']}: {t['title']} ({t['url']}) {'[ACTIVE]' if t.get('active') else ''}\n"
                    for t in result
                )

            return str(result)

        except Exception as e:
            logger.exception(
                "Browser tool execution failed: "
                f"{function_name} args={redact_sensitive_text(args)}"
            )
            return f"Error executing browser tool: {e}"

    def _browser_skill_enabled(self) -> bool:
        try:
            skills = getattr(getattr(self.config, "skills", None), "enabled", []) or []
            return "browser" in skills
        except Exception:
            return False

    async def _gather_search(
        self,
        query: str,
        count: int,
        kind: str,
        session_key: str,
        on_progress=None,
    ):
        """Run the provider chain (+ browser scrape fallback for web/news).

        Returns a ``SearchResponse`` on success, or ``None`` with the last error
        string via the second tuple element.
        """
        from core.web_search import build_provider_chain

        last_err = ""
        for provider in build_provider_chain(self.config):
            try:
                if on_progress:
                    await on_progress(
                        f"🔍 Searching ({provider.name}) for: {query}"
                    )
                resp = await provider.search(query, count=count, kind=kind)
                if resp.ok:
                    return resp, ""
                last_err = resp.error or "no results"
            except Exception as e:  # provider crash → try the next one
                last_err = str(e)
                logger.warning(f"Search provider {provider.name} failed: {e}")

        # Final fallback: scrape Google via the live browser (web/news only).
        if kind in ("web", "news") and self._browser_skill_enabled():
            try:
                if on_progress:
                    await on_progress("🔍 Falling back to browser Google search...")
                browser = await get_browser_manager(
                    session_key=session_key, config=self.config
                )
                raw = await browser.google_search(query, on_progress=on_progress)
                raw_results = raw.get("results") if isinstance(raw, dict) else None
                if (
                    isinstance(raw, dict)
                    and raw.get("success")
                    and isinstance(raw_results, list)
                    and raw_results
                ):
                    from core.web_search import SearchResponse, SearchResult

                    scraped = SearchResponse(
                        kind=kind, query=query, provider="google-scrape"
                    )
                    for item in raw_results[:count]:
                        if not isinstance(item, dict):
                            continue
                        url = str(item.get("url") or "").strip()
                        if not url:
                            continue
                        scraped.results.append(
                            SearchResult(
                                title=str(item.get("title") or url),
                                url=url,
                                snippet=str(item.get("snippet") or ""),
                                source="google-scrape",
                            )
                        )
                    if scraped.results:
                        return scraped, ""
                if isinstance(raw, dict):
                    last_err = str(raw.get("error") or "no parseable Google results")
            except Exception as e:
                last_err = str(e)
                logger.warning(f"Browser google_search fallback failed: {e}")

        return None, last_err

    @staticmethod
    def _artifact_delivery_requested(text: str) -> bool:
        """Return whether the user expects a concrete file/download outcome."""
        return bool(_ARTIFACT_REQUEST_RE.search(str(text or "")))

    @staticmethod
    def _artifact_reserve_tool_definitions(
        tool_definitions: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Remove open-ended research tools while preserving creation/delivery tools."""
        if not tool_definitions:
            return tool_definitions
        reserved = []
        for definition in tool_definitions:
            name = str(definition.get("function", {}).get("name") or "")
            if name not in _RESEARCH_TOOL_NAMES:
                reserved.append(definition)
        return reserved

    async def _execute_search_tool(
        self, function_name: str, args: Dict[str, Any], session_key: str
    ) -> str:
        from core.web_search import (
            DEFAULT_COUNT,
            MAX_COUNT,
            format_search_response,
        )

        query = str(args.get("query") or "").strip()
        if not query:
            return "Error: a search query is required."

        if function_name == "deep_research":
            return await self._run_deep_research(query, args, session_key)

        if function_name == "image_search":
            kind = "images"
        else:
            raw_kind = str(args.get("kind") or "web").strip().lower()
            kind = "news" if raw_kind == "news" else "web"

        try:
            count = max(1, min(int(args.get("count") or DEFAULT_COUNT), MAX_COUNT))
        except (TypeError, ValueError):
            count = DEFAULT_COUNT

        on_progress = self.toolbox.send_progress
        response, last_err = await self._gather_search(
            query, count, kind, session_key, on_progress=on_progress
        )
        if response is None:
            return (
                f"Error: web search failed ({last_err or 'no provider available'}). "
                "Configure a search API key (TAVILY_API_KEY / BRAVE_SEARCH_API_KEY / "
                "SERPAPI_API_KEY) or enable the browser skill. Do not repeat web_search "
                "with rephrased queries in this turn; navigate to one known official URL "
                "or continue with explicit caveats."
            )
        return format_search_response(response)

    async def _run_deep_research(
        self, query: str, args: Dict[str, Any], session_key: str
    ) -> str:
        on_progress = self.toolbox.send_progress
        await on_progress(f"🧪 Deep research: {query}")

        response, last_err = await self._gather_search(
            query, count=6, kind="web", session_key=session_key, on_progress=on_progress
        )
        if response is None or not response.results:
            # Nothing structured to read; surface any provider answer we did get.
            if response is not None and response.answer:
                return response.answer
            return (
                f"Error: deep_research could not gather sources for '{query}' "
                f"({last_err or 'no results'})."
            )

        MAX_SOURCES = 5
        MAX_CHARS_PER_SOURCE = 4000
        sources: List[Dict[str, str]] = []
        seen: Set[str] = set()
        for r in response.results:
            if len(sources) >= MAX_SOURCES:
                break
            if not r.url or r.url in seen:
                continue
            seen.add(r.url)
            content = r.content or ""
            if len(content) < 400:
                await on_progress(f"📖 Reading: {r.url}")
                fetched = await self.toolbox.fetch_readable_text(
                    r.url, max_chars=MAX_CHARS_PER_SOURCE
                )
                if fetched and not fetched.startswith("Error:"):
                    content = fetched
            sources.append(
                {
                    "title": r.title or r.url,
                    "url": r.url,
                    "content": (content or r.snippet or "")[:MAX_CHARS_PER_SOURCE],
                }
            )

        if not sources:
            return response.answer or (
                f"Error: deep_research found results but could not read any source "
                f"for '{query}'."
            )

        numbered = "\n\n".join(
            f"[{i}] {s['title']} — {s['url']}\n{s['content']}"
            for i, s in enumerate(sources, 1)
        )
        synthesis = await self._synthesize_research(
            query, numbered, response.answer, session_key
        )
        sources_md = "\n".join(
            f"[{i}] {s['title']} — {s['url']}" for i, s in enumerate(sources, 1)
        )
        return f"{synthesis}\n\n**Sources:**\n{sources_md}"

    async def _synthesize_research(
        self, query: str, numbered_sources: str, provider_answer: str, session_key: str
    ) -> str:
        system = (
            "You are a meticulous research assistant. Using ONLY the numbered "
            "sources provided, write a concise, well-structured answer to the "
            "user's question. Cite sources inline with bracketed numbers like [1], "
            "[2] that correspond to the source list. If the sources conflict or are "
            "insufficient, say so explicitly. Never invent facts, URLs, or citations."
        )
        user = f"Question: {query}\n\n"
        if provider_answer:
            user += f"Provider summary (for context, still cite sources): {provider_answer}\n\n"
        user += f"Sources:\n{numbered_sources}"

        try:
            provider = self.llm_client.resolve_provider(
                self.model,
                default_base_url=self.config.llm.base_url,
            )
            resp = await self.llm_client.complete(
                provider,
                ChatRequest(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=1000,
                    session_id=f"{session_key}::deep-research",
                ),
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as e:
            logger.warning(f"deep_research synthesis failed: {e}")
        return provider_answer or "Research synthesis unavailable; see sources below."

    async def _execute_tag_compat_tool(
        self, function_name: str, function_args: Dict[str, Any], session_key: str = "default"
    ) -> str:
        content = ""
        # Check explicit priority keys first
        for key in ["content", "entry", "text", "context", "mood", "soul", "identity", "relationship", "value"]:
            if key in function_args and function_args[key]:
                content = str(function_args[key]).strip()
                break

        # Fallback: find the first non-empty string value in function_args
        if not content:
            for k, v in function_args.items():
                if isinstance(v, str) and v.strip():
                    content = v.strip()
                    break

        if not content:
            return f"Error: '{function_name}' requires content"

        # Resolve sender_id safely
        sender_id = None
        from core.context import tool_context
        ctx = tool_context.get() or {}
        if ctx.get("sender_id"):
            sender_id = ctx["sender_id"]
        elif ":" in session_key:
            sender_id = session_key.split(":", 1)[1]
        
        if not sender_id:
            sender_id = "tool-compat"

        raw_reply = f"<{function_name}>{content}</{function_name}>"
        tag_result = await process_tags(
            raw_reply=raw_reply,
            sender_id=sender_id,
            validate_soul=prompt_module.validate_and_save_soul,
            validate_identity=prompt_module.validate_and_save_identity,
            validate_mood=prompt_module.validate_and_save_mood,
            validate_relationship=prompt_module.validate_and_save_relationships,
            vector_service=self.vector_service,
            bus=self.bus,
            msg=None,
            config=self.config,
        )

        if tag_result.soul_updated or tag_result.identity_updated:
            self._invalidate_stable_prompt(sender_id)

        if function_name == "save_memory":
            return "Long-term memory saved."
        if function_name == "log_memory":
            return "Memory logged."
        if function_name == "save_soul":
            return "Soul saved."
        if function_name == "save_identity":
            return "Identity saved."
        if function_name == "save_mood":
            return "Mood saved."
        if function_name == "save_relationship":
            return "Relationship saved."
        if function_name == "save_user":
            return "User profile saved."
        return "Tag action completed."

    async def _execute_background_task_tool(
        self, function_name: str, function_args: Dict[str, Any]
    ) -> str:
        raw_ids = function_args.get("task_ids")
        if raw_ids is None and function_args.get("task_id") is not None:
            raw_ids = [function_args.get("task_id")]
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        task_ids: List[str] = []
        seen: Set[str] = set()
        for value in raw_ids or []:
            task_id = str(value or "").strip()
            if task_id and task_id not in seen:
                task_ids.append(task_id)
                seen.add(task_id)
            if len(task_ids) >= 20:
                break
        if not task_ids:
            return "Error: at least one background task_id is required."

        try:
            timeout_ms = max(0, min(int(function_args.get("timeout_ms") or 0), 300_000))
        except (TypeError, ValueError):
            timeout_ms = 0
        timeout_s = timeout_ms / 1000.0

        if function_name == "kill_task":
            task = await self.kill_background_subagent_task(task_ids[0])
            if task is None:
                return f"Task not found: {task_ids[0]}"
            if task.status == "cancelled":
                return f"Task {task.task_id} killed (cancelled)."
            return f"Task {task.task_id} already exited with status {task.status}."

        outputs: List[str] = []
        for task_id in task_ids:
            try:
                output = await self.get_background_subagent_output(
                    task_id,
                    timeout=timeout_s if timeout_s > 0 else None,
                )
            except asyncio.TimeoutError:
                output = f"Task {task_id}\nStatus: running\nWait timed out."
            if output is None:
                outputs.append(f"Task not found: {task_id}")
            else:
                outputs.append(output)
        return "\n\n".join(outputs)

    @staticmethod
    def _capability_match_score(query: str, name: str, description: str = "") -> int:
        tokens = {
            token
            for token in re.findall(r"[a-z0-9_/-]+", str(query or "").lower())
            if len(token) >= 2
        }
        if not tokens:
            return 1
        haystack = {
            token
            for token in re.findall(
                r"[a-z0-9_/-]+", f"{name} {description}".lower()
            )
            if len(token) >= 2
        }
        score = len(tokens & haystack)
        if str(name or "").lower() in str(query or "").lower():
            score += 50
        return score

    def _resolve_capabilities(
        self,
        query: str = "",
        include_disabled: bool = True,
        session_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a redacted capability resolution snapshot for tools and API."""
        registry = getattr(self, "skill_registry", None)
        skill_rows: list[Dict[str, Any]] = []
        matched_skills: list[str] = []
        required_tools: list[str] = []
        revision = int(getattr(registry, "capability_revision", 0) or 0)
        if registry is not None:
            try:
                skill_rows = registry.search_capabilities(
                    query, include_inactive=include_disabled, limit=12
                )
            except Exception as exc:
                logger.debug(f"Capability skill search failed: {exc}")
            context = self._get_capability_turn_context(session_key, query)
            matched_skills = list(context.get("skill_names") or [])
            if hasattr(registry, "get_required_tool_names_for_skills"):
                required_tools = registry.get_required_tool_names_for_skills(
                    matched_skills
                )

        all_tools = list(self._get_tool_definitions())
        tool_rows: list[Dict[str, Any]] = []
        for tool in all_tools:
            function = tool.get("function") if isinstance(tool, dict) else {}
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            description = redact_sensitive_text(
                re.sub(r"\s+", " ", str(function.get("description") or "")).strip()
            )[:180]
            score = self._capability_match_score(query, name, description)
            if str(query or "").strip() and score <= 0:
                continue
            if name.startswith("mcp_"):
                parts = name.split("_", 2)
                server = parts[1] if len(parts) > 1 else "unknown"
                try:
                    from core.mcp_client import get_mcp_manager

                    connection_state = get_mcp_manager().get_status().get(
                        server, "Offline"
                    )
                except Exception:
                    connection_state = "unknown"
                state = str(connection_state).lower()
                tool_type = "mcp_tool"
            else:
                state = "ready"
                tool_type = "native_tool"
            tool_rows.append(
                {
                    "name": name,
                    "type": tool_type,
                    "description": description,
                    "state": state,
                    "match_score": score,
                }
            )

        # Keep configured MCP servers visible even when they currently expose
        # no cached tools (for example during a reconnect or failed handshake).
        try:
            from core.mcp_client import get_mcp_manager

            mcp_status = get_mcp_manager().get_status()
            for server, connection_state in sorted((mcp_status or {}).items()):
                score = self._capability_match_score(query, str(server), "MCP server")
                if str(query or "").strip() and score <= 0:
                    continue
                tool_rows.append(
                    {
                        "name": str(server),
                        "type": "mcp_server",
                        "description": "Configured MCP server",
                        "state": str(connection_state).lower(),
                        "match_score": score,
                    }
                )
        except Exception:
            pass

        subagent_rows: list[Dict[str, Any]] = []
        subagent_registry = getattr(self, "subagent_registry", None)
        if subagent_registry is not None:
            try:
                descriptions = subagent_registry.get_agent_descriptions()
            except Exception:
                descriptions = {}
            for name, description in sorted((descriptions or {}).items()):
                score = self._capability_match_score(query, name, description)
                if str(query or "").strip() and score <= 0:
                    continue
                subagent_rows.append(
                    {
                        "name": str(name),
                        "type": "subagent",
                        "description": redact_sensitive_text(
                            re.sub(r"\s+", " ", str(description or "")).strip()
                        )[:180],
                        "state": "ready",
                        "match_score": score,
                    }
                )

        rows = skill_rows + tool_rows + subagent_rows
        rows.sort(
            key=lambda item: (
                -int(item.get("match_score", 0) or 0),
                str(item.get("type") or ""),
                str(item.get("name") or ""),
            )
        )
        selected_tools = self._get_tool_definitions_for_turn(
            query, session_key=session_key
        )
        selected_tool_names = self._tool_definition_names(selected_tools)
        ready_matches = [
            row
            for row in rows
            if row.get("state") in {"ready", "online", "registered"}
        ]
        if ready_matches:
            overall_state = "ready"
            reason = "At least one registered, enabled, or connected capability matched the request."
        elif skill_rows:
            overall_state = str(skill_rows[0].get("state") or "unavailable")
            reason = "A discovered capability matched, but it is not currently operational."
        elif rows:
            overall_state = str(rows[0].get("state") or "unavailable")
            reason = "A capability matched, but it is not currently operational."
        else:
            overall_state = "unavailable"
            reason = "No discovered capability matched this query; use the exact integration name or inspect the inventory."
        return {
            "query": str(query or ""),
            "revision": revision,
            "state": overall_state,
            "reason": reason,
            "matched_skills": matched_skills,
            "required_tools": required_tools,
            "selected_tools": selected_tool_names,
            "capabilities": rows[:30],
        }

    async def _execute_capability_search(
        self, function_args: Dict[str, Any], session_key: str
    ) -> str:
        query = str(function_args.get("query") or "").strip()[:1_000]
        include_disabled = bool(function_args.get("include_disabled", True))
        result = self._resolve_capabilities(
            query,
            include_disabled=include_disabled,
            session_key=session_key,
        )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    def resolve_capabilities(
        self, text: str = "", session_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Public read-only resolver used by the dashboard diagnostic endpoint."""
        return self._resolve_capabilities(
            str(text or "")[:1_000],
            include_disabled=True,
            session_key=str(session_key or "").strip()[:180] or None,
        )

    async def _execute_tool(
        self, function_name: str, function_args: dict, session_key: str
    ) -> Any:

        cached = self.tool_cache.get(function_name, function_args)
        if cached:
            logger.debug(f"⚡ Cache hit: {function_name}")
            return cached

        # ── Intercept echo-as-memory ─────────────────────────────────

        if function_name == "run_command":
            cmd = function_args.get("command", "")
            echo_match = re.match(r'^echo\s+["\'](.+?)["\']\s*$', cmd, re.DOTALL)
            if echo_match:
                entry = echo_match.group(1).strip()
                if len(entry) > 10:
                    try:
                        # Keep the legacy echo compatibility path on the same
                        # Markdown-first writer as the native memory_save tool.
                        # This also honors LIMEBOT_STATE_DIR and queues optional
                        # vector indexing without making it a prerequisite.
                        result = await self.toolbox.memory_save(entry, scope="journal")
                        logger.info(f"Redirected echo to log_memory: {entry[:80]}")
                        return result
                    except Exception as e:
                        logger.error(f"Error in echo-to-log_memory redirect: {e}")

        try:
            if function_name == "spawn_agent":
                result = await self.toolbox.spawn_agent(
                    session_key=session_key, **function_args
                )
            elif function_name == "capability_search":
                result = await self._execute_capability_search(
                    function_args, session_key
                )
            elif function_name in {"get_task_output", "wait_tasks", "kill_task"}:
                result = await self._execute_background_task_tool(
                    function_name, function_args
                )
            elif function_name in _SEARCH_TOOLS:
                result = await self._execute_search_tool(
                    function_name, function_args, session_key
                )
            elif function_name.startswith("browser_"):
                result = await self._execute_browser_tool(
                    function_name, function_args, session_key
                )
            elif function_name.startswith("mcp_"):
                from core.mcp_client import get_mcp_manager

                result = await get_mcp_manager().execute_tool(
                    function_name, function_args
                )
            elif function_name in _TAG_COMPAT_TOOLS:
                result = await self._execute_tag_compat_tool(
                    function_name, function_args, session_key
                )
            else:
                handler = self._tool_registry.get(function_name)
                if handler:
                    result = await handler(**function_args)
                else:
                    self.metrics.record_anomaly(
                        session_key,
                        "unknown_tool",
                        detail=f"{function_name}({function_args})",
                    )
                    result = f"Error: Unknown tool '{function_name}'"

            is_read_only = function_name in _BROWSER_CACHEABLE or function_name in {
                "capability_search",
                "read_file",
                "list_dir",
                "search_files",
                "verify_files",
                "diagnose_files",
                "memory_search",
                "web_search",
                "image_search",
            }
            if result and not str(result).startswith("Error:") and is_read_only:
                self.tool_cache.set(function_name, function_args, result)

            if (
                function_name
                in {
                    "edit_file",
                    "write_file",
                    "delete_file",
                    "create_skill",
                    "run_command",
                }
                and result
                and not str(result).startswith("Error:")
            ):
                # File/system mutations can stale read/list/search caches.
                self.tool_cache.clear()

            return result
        except Exception as e:
            return f"Error executing '{function_name}': {e}"

    def _tool_execution_timeout(self, function_name: str) -> Optional[float]:
        """Return the outer LimeBot deadline for a native tool invocation."""
        if function_name == "generate_image":
            # Image providers can legitimately take several minutes, especially
            # for high-quality reference edits and fallback attempts. Their own
            # request timeouts still bound individual network operations.
            return None
        if function_name == "analyze_video":
            return 600.0
        return getattr(self.config, "tool_timeout", 120.0)

    @staticmethod
    def _extract_run_command_exit_code(result: Any) -> Optional[int]:
        match = re.search(r"Exit Code:\s*(-?\d+)", str(result))
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    def _is_tool_result_error(self, function_name: str, result: Any) -> bool:
        text = str(result or "")

        if text.startswith("Error:") or text.startswith("Error executing"):
            return True
        if text.startswith("ACTION CANCELLED:") or text.startswith("ACTION BLOCKED:"):
            return True
        if "[TIMEOUT]" in text or "[STALL]" in text:
            return True

        if function_name == "run_command":
            exit_code = self._extract_run_command_exit_code(text)
            if exit_code not in (None, 0):
                return True

        if function_name in {"edit_file", "verify_files", "diagnose_files"}:
            try:
                payload = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                if payload.get("status") == "failed":
                    return True
                verification = payload.get("verification")
                if (
                    function_name == "edit_file"
                    and isinstance(verification, dict)
                    and verification.get("status") == "failed"
                ):
                    return True

        return False

    @staticmethod
    def _is_coding_turn(content: str, tool_calls: Optional[List[Dict[str, Any]]] = None) -> bool:
        if _CODING_HINT_RE.search(str(content or "")):
            return True
        for tool_call in tool_calls or []:
            function = tool_call.get("function") if isinstance(tool_call, dict) else {}
            name = str((function or {}).get("name") or "")
            if name in _MUTATION_TOOL_NAMES or name == "run_command":
                return True
        return False

    @staticmethod
    def _tool_phase(function_name: str, function_args: Dict[str, Any]) -> str:
        if function_name in {"verify_files", "diagnose_files"}:
            return "verify"
        if function_name in _MUTATION_TOOL_NAMES:
            return "apply"
        if function_name == "run_command":
            command = str((function_args or {}).get("command") or "").lower()
            if re.search(r"\b(test|pytest|unittest|lint|build|compile|check|verify)\b", command):
                return "verify"
            return "apply"
        return "inspect"

    def _build_tool_outcome(self, function_name: str, result: Any) -> ToolOutcome:
        text = str(result or "")
        exit_code = self._extract_run_command_exit_code(text)
        timed_out = "[TIMEOUT]" in text or "timed out" in text.lower()
        stalled = "[STALL]" in text
        success = not self._is_tool_result_error(function_name, text)
        normalized = re.sub(r"\d+", "#", text.lower())[:2000]
        fingerprint = ""
        if not success:
            fingerprint = hashlib.sha256(
                f"{function_name}:{normalized}".encode("utf-8", "replace")
            ).hexdigest()[:16]
        verification_status: Optional[str] = None
        verification_detail = ""
        if function_name in {"edit_file", "verify_files", "diagnose_files"}:
            try:
                payload = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                verification = payload.get("verification")
                if function_name == "verify_files":
                    verification = payload
                if isinstance(verification, dict):
                    status = str(verification.get("status") or "").strip().lower()
                    if status:
                        verification_status = status
                    verification_detail = truncate_tool_result(
                        redact_sensitive_text(json.dumps(verification, ensure_ascii=False)),
                        600,
                    )
        diagnostic_limit = 800
        diagnostic = truncate_tool_result(redact_sensitive_text(text), diagnostic_limit)
        split = diagnostic.split("\n... [truncated diagnostic] ...\n", 1)
        return ToolOutcome(
            tool=function_name,
            success=success,
            exit_code=exit_code,
            timed_out=timed_out,
            stalled=stalled,
            retry_safe=function_name in _READ_ONLY_TOOL_NAMES,
            failure_fingerprint=fingerprint,
            diagnostic_head=split[0][:400],
            diagnostic_tail=(split[1] if len(split) == 2 else split[0])[-400:],
            verification_status=verification_status,
            verification_detail=verification_detail,
        )

    async def _emit_coding_phase(
        self,
        phase: str,
        session_key: str,
        msg: Optional[InboundMessage],
        *,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
        outcome: Optional[ToolOutcome] = None,
    ) -> None:
        """Emit a bounded progress event; this deliberately has no terminal signal."""
        if phase not in CODING_PHASES:
            return
        payload: Dict[str, Any] = {"type": "coding_phase", "phase": phase}
        event: Dict[str, Any] = {"type": "coding_phase", "phase": phase}
        if outcome:
            details = {
                "tool": outcome.tool,
                "success": outcome.success,
                "exit_code": outcome.exit_code,
                "timed_out": outcome.timed_out,
                "stalled": outcome.stalled,
                "retry_safe": outcome.retry_safe,
                "failure_fingerprint": outcome.failure_fingerprint,
                "diagnostic_head": outcome.diagnostic_head,
                "diagnostic_tail": outcome.diagnostic_tail,
                "verification_status": outcome.verification_status,
                "verification_detail": outcome.verification_detail,
            }
            payload["outcome"] = details
            event["outcome"] = details
        self._log_session_event(session_key, event)
        await self._publish_both(
            msg,
            "",
            self._with_trace_metadata(payload, turn_id=turn_id, message_id=message_id),
        )

    def _coding_recovery_message(self, outcome: ToolOutcome, attempt: int, budget: int) -> str:
        return (
            "Coding verification failed. Continue deliberately: inspect the relevant "
            "failure, apply one targeted edit only if justified, then rerun a narrowed "
            "verification command; otherwise report a blocker. Do not blindly retry a "
            "mutation. "
            f"Repair attempt {attempt}/{budget}. Tool: {outcome.tool}. "
            f"Exit code: {outcome.exit_code!r}. Diagnostic tail: "
            f"{redact_sensitive_text(outcome.diagnostic_tail)}"
        )

    def _tool_recovery_message(
        self,
        outcome: ToolOutcome,
        attempt: int,
        budget: int,
        *,
        repeated: bool = False,
    ) -> str:
        repeated_note = (
            " The same failure fingerprint appeared before; choose a materially "
            "different diagnostic or route."
            if repeated
            else ""
        )
        return (
            "A tool operation failed while pursuing the user's original goal. "
            "Continue toward that goal instead of finalizing a blocker. Classify "
            "the failure (local/client, server, authentication, permission, or "
            "policy), inspect the evidence, and try a different safe route or "
            "diagnostic. Do not disable security controls and do not repeat the "
            "identical failing call unless new evidence justifies it. Only report "
            "the goal as blocked after the blocker is independently confirmed and "
            "reasonable safe alternatives are exhausted."
            f"{repeated_note} Recovery attempt {attempt}/{budget}. "
            f"Tool: {outcome.tool}. Diagnostic tail: "
            f"{redact_sensitive_text(outcome.diagnostic_tail)}"
        )

    def _has_successful_mutation_since_previous_verifier_failure(
        self, session_key: str
    ) -> bool:
        """Allow one repeated failure only when an edit succeeded between failures."""
        entries = self.history.get(session_key, [])
        failure_indexes = [
            index
            for index, entry in enumerate(entries)
            if entry.get("role") == "tool"
            and entry.get("name") == "run_command"
            and self._is_tool_result_error("run_command", entry.get("content", ""))
        ]
        if len(failure_indexes) < 2:
            return False
        start = failure_indexes[-2] + 1
        end = failure_indexes[-1]
        return any(
            entry.get("role") == "tool"
            and entry.get("name") in _MUTATION_TOOL_NAMES
            and not self._is_tool_result_error(
                str(entry.get("name")), entry.get("content", "")
            )
            for entry in entries[start:end]
        )

    def _build_step_cap_fallback(self, session_key: str) -> str:
        recent = [
            entry for entry in self.history.get(session_key, []) if entry.get("role") == "tool"
        ][-2:]
        if not recent:
            return "The coding step limit was reached before verification could run."
        details = "; ".join(
            f"{entry.get('name', 'tool')}: "
            f"{redact_sensitive_text(str(entry.get('content', ''))[-220:])}"
            for entry in recent
        )
        return (
            "The coding step limit was reached. I ran: " + details +
            ". Remaining work: inspect the last diagnostic and run the narrowed verification."
        )

    async def _queue_tool_recovery(
        self,
        session_key: str,
        msg: Optional[InboundMessage],
        fingerprints: Set[str],
        attempts: int,
        *,
        coding_turn: bool = False,
        allowed_tools: Optional[Set[str]] = None,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Tuple[int, Optional[str]]:
        """Queue one model-directed recovery step, or return a concrete blocker."""
        failures = [
            outcome
            for outcome in getattr(self, "_last_tool_outcomes", [])
            if not outcome.success
            and (allowed_tools is None or outcome.tool in allowed_tools)
        ]
        if not failures:
            return attempts, None
        failure = failures[-1]
        budget = max(
            1,
            int(
                getattr(
                    self.config,
                    "tool_recovery_max_attempts",
                    getattr(self.config, "coding_repair_max_attempts", 3),
                )
                or 3
            ),
        )
        repeated = failure.failure_fingerprint in fingerprints
        attempts += 1
        fingerprints.add(failure.failure_fingerprint)
        if attempts > budget:
            blocked = (
                f"Tool recovery budget ({budget}) is exhausted. The original goal "
                "remains incomplete. Last diagnostic: "
                f"{redact_sensitive_text(failure.diagnostic_tail)}"
            )
            if coding_turn:
                await self._emit_coding_phase(
                    "blocked", session_key, msg, turn_id=turn_id,
                    message_id=message_id, outcome=failure
                )
            else:
                self._log_session_event(
                    session_key,
                    {
                        "type": "tool_recovery_blocked",
                        "tool": failure.tool,
                        "failure_fingerprint": failure.failure_fingerprint,
                        "diagnostic": redact_sensitive_text(failure.diagnostic_tail),
                    },
                )
            return attempts, blocked
        recovery_message = (
            self._coding_recovery_message(failure, attempts, budget)
            if coding_turn
            else self._tool_recovery_message(
                failure, attempts, budget, repeated=repeated
            )
        )
        self.history[session_key].append(
            {
                "role": "system",
                "content": recovery_message,
            }
        )
        self._mark_dirty(session_key)
        if coding_turn:
            await self._emit_coding_phase(
                "repair", session_key, msg, turn_id=turn_id,
                message_id=message_id, outcome=failure
            )
        else:
            self._log_session_event(
                session_key,
                {
                    "type": "tool_recovery_queued",
                    "tool": failure.tool,
                    "attempt": attempts,
                    "budget": budget,
                    "repeated_failure": repeated,
                },
            )
        return attempts, None

    async def _queue_coding_recovery(
        self,
        session_key: str,
        msg: Optional[InboundMessage],
        fingerprints: Set[str],
        attempts: int,
        *,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Tuple[int, Optional[str]]:
        """Compatibility wrapper for coding-specific callers and tests."""
        return await self._queue_tool_recovery(
            session_key,
            msg,
            fingerprints,
            attempts,
            coding_turn=True,
            allowed_tools={"run_command"},
            turn_id=turn_id,
            message_id=message_id,
        )

    def _build_tool_fallback_reply(self, session_key: str, max_items: int = 2) -> str:
        tool_rows: List[Tuple[str, str]] = []
        for entry in reversed(self.history.get(session_key, [])):
            if entry.get("role") != "tool":
                continue
            name = str(entry.get("name") or "tool")
            content = str(entry.get("content") or "").strip()
            tool_rows.append((name, content))
            if len(tool_rows) >= max_items:
                break

        if not tool_rows:
            return "I finished running the tool, but I don't have a follow-up response yet."

        tool_rows.reverse()
        failed_rows: List[Tuple[str, str]] = []
        for name, content in tool_rows:
            if not self._is_tool_result_error(name, content):
                continue
            first_line = next(
                (ln.strip() for ln in content.splitlines() if ln.strip()),
                "Tool execution failed.",
            )
            if len(first_line) > 180:
                first_line = first_line[:180] + "..."
            first_line = redact_sensitive_text(first_line)
            failed_rows.append((name, first_line))

        if failed_rows:
            lines = "\n".join([f"- `{name}`: {line}" for name, line in failed_rows])
            return "I ran the requested tool(s), but they failed:\n" + lines

        recent_tools = ", ".join(f"`{name}`" for name, _ in tool_rows)
        return (
            "I finished the requested tool step(s) successfully, but I did not produce "
            f"a natural-language wrap-up. Recent steps: {recent_tools}."
        )

    @staticmethod
    def _is_read_only_plan_request(content: str, msg: Optional[InboundMessage]) -> bool:
        metadata = getattr(msg, "metadata", {}) if msg else {}
        requested_mode = str((metadata or {}).get("coding_mode") or "").lower()
        return requested_mode == "plan" or str(content or "").strip().lower().startswith("/plan")

    @staticmethod
    def _workspace_id_from_message(msg: Optional[InboundMessage]) -> str:
        metadata = getattr(msg, "metadata", {}) if msg else {}
        return str((metadata or {}).get("workspace_id") or "").strip()

    async def _publish_changeset(
        self,
        changeset: Dict[str, Any],
        msg: Optional[InboundMessage],
        *,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        await self._publish_both(
            msg,
            "",
            self._with_trace_metadata(
                {"type": "changeset", "changeset": changeset},
                turn_id=turn_id,
                message_id=message_id,
            ),
        )

    async def _stage_workspace_changeset(
        self,
        tool_calls: List[Dict[str, Any]],
        session_key: str,
        msg: Optional[InboundMessage],
        *,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create one redacted review artifact before a coding mutation runs."""
        write_previews: List[Dict[str, Any]] = []
        for tool_call in tool_calls or []:
            _, name, args = self._parse_tool_call(tool_call, session_key)
            if name not in _MUTATION_TOOL_NAMES:
                continue
            write_previews.append(self._build_confirmation_preview(name, args, session_key))
        if not write_previews:
            return None

        from core.review_entrypoint import build_changeset_artifact, changeset_for_app

        diff_parts: List[str] = []
        for index, preview in enumerate(write_previews):
            diff = str(preview.get("diff") or "").strip()
            if diff:
                diff_parts.append(diff)
                continue
            path = str(preview.get("path") or f"file-{index + 1}")
            mode = str(preview.get("mode") or preview.get("kind") or "change")
            if mode == "delete":
                diff_parts.append(
                    f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ /dev/null\n@@ -1 +0,0 @@\n- [content redacted]"
                )
            else:
                diff_parts.append(
                    f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+ [content redacted]"
                )
        changeset = build_changeset_artifact(
            "\n".join(diff_parts),
            status="awaiting_approval",
            summary=f"{len(write_previews)} file change(s) are staged for review.",
        )
        changeset["id"] = f"changeset-{turn_id or uuid.uuid4().hex[:12]}"
        workspace_id = self._workspace_id_from_message(msg)
        if workspace_id:
            from core.task_tracker import get_task_tracker

            artifact = await get_task_tracker().add_workspace_artifact(
                workspace_id,
                kind="change_set",
                title="Patch review",
                metadata=changeset,
            )
            if artifact:
                self._workspace_changesets[session_key] = (
                    workspace_id,
                    artifact.artifact_id,
                )
        await self._publish_changeset(
            changeset_for_app(changeset),
            msg,
            turn_id=turn_id,
            message_id=message_id,
        )
        return changeset

    async def _refresh_workspace_changeset(
        self,
        session_key: str,
        msg: Optional[InboundMessage],
        outcome: ToolOutcome,
        *,
        blocked: bool = False,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        stored = self._workspace_changesets.get(session_key)
        if not stored:
            return
        workspace_id, artifact_id = stored
        from core.review_entrypoint import changeset_for_app
        from core.task_tracker import get_task_tracker

        tracker = get_task_tracker()
        workspace = await tracker.get_workspace(workspace_id)
        if workspace is None:
            return
        artifact = next(
            (item for item in workspace.artifacts if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None or artifact.kind != "change_set":
            return
        changeset = dict(artifact.metadata)
        verification = list(changeset.get("verification") or [])
        if outcome.tool == "run_command":
            verification.append(
                {
                    "id": f"verification-{len(verification) + 1}",
                    "label": "Verification",
                    "status": "passed" if outcome.success else "failed",
                    "exit_code": outcome.exit_code,
                    "diagnostic": outcome.diagnostic_tail,
                }
            )
        elif outcome.verification_status:
            verification.append(
                {
                    "id": f"verification-{len(verification) + 1}",
                    "label": "Native file checks",
                    "status": "passed" if outcome.verification_status == "passed" else "failed",
                    "diagnostic": outcome.verification_detail,
                }
            )
        if blocked:
            changeset["status"] = "blocked"
        elif outcome.tool == "run_command":
            changeset["status"] = "verified" if outcome.success else "failed"
        elif outcome.verification_status == "failed":
            changeset["status"] = "failed"
        elif outcome.success:
            changeset["status"] = "applied"
        changeset["verification"] = verification
        updated = await tracker.update_workspace_artifact(
            workspace_id,
            artifact_id,
            metadata_update=changeset,
        )
        if updated:
            await self._publish_changeset(
                changeset_for_app(changeset),
                msg,
                turn_id=turn_id,
                message_id=message_id,
            )

    async def _persist_coding_plan(
        self,
        plan_text: str,
        msg: Optional[InboundMessage],
        *,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        from core.review_entrypoint import build_coding_plan_artifact

        artifact_data = build_coding_plan_artifact(plan_text)
        workspace_id = self._workspace_id_from_message(msg)
        if workspace_id:
            from core.task_tracker import get_task_tracker

            await get_task_tracker().add_workspace_artifact(
                workspace_id,
                kind="coding_plan",
                title="Coding plan",
                metadata=artifact_data,
            )
        await self._publish_both(
            msg,
            "",
            self._with_trace_metadata(
                {"type": "coding_plan", "plan": artifact_data},
                turn_id=turn_id,
                message_id=message_id,
            ),
        )

    @staticmethod
    def _build_empty_reply_fallback() -> str:
        return (
            "I processed that, but I failed to produce a visible reply. "
            "Please ask again if you still need the answer."
        )

    def _parse_tool_call(
        self, tool_call: dict, session_key: str
    ) -> Tuple[str, str, Dict[str, Any]]:
        tc_id = tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}"
        tool_call["id"] = tc_id
        function = tool_call.get("function")
        if not isinstance(function, dict):
            function = {"name": "", "arguments": "{}"}
            tool_call["function"] = function

        function_name = function.get("name", "")
        raw_args = function.get("arguments", "{}")

        if isinstance(raw_args, dict):
            function_args = raw_args
            raw_args = json.dumps(raw_args)
            function["arguments"] = raw_args
        elif raw_args in (None, ""):
            raw_args = "{}"
            function["arguments"] = raw_args

        if raw_args == "{}{}":
            logger.warning(f"Malformed args for {function_name} - fixing to {{}}")
            self.metrics.record_anomaly(
                session_key,
                "malformed_tool_args",
                detail=f"{function_name}:double_object",
            )
            raw_args = "{}"
            function["arguments"] = raw_args

        if not isinstance(raw_args, str):
            raw_args_type = type(raw_args).__name__
            function_args = {}
            raw_args = "{}"
            function["arguments"] = raw_args
            logger.error(
                f"Invalid non-string args for '{function_name}'. Using {{}}."
            )
            self.metrics.record_anomaly(
                session_key,
                "invalid_tool_args_type",
                detail=f"{function_name}:{raw_args_type}",
            )
        else:
            raw_args_detail = raw_args
            try:
                function_args = json.loads(raw_args)
            except json.JSONDecodeError:
                function_args = {}
                raw_args = "{}"
                function["arguments"] = raw_args
                logger.error(f"Invalid JSON args for '{function_name}'. Using {{}}.")
                self.metrics.record_anomaly(
                    session_key,
                    "invalid_tool_args_json",
                    detail=f"{function_name}:{raw_args_detail[:120]}",
                )

        if not isinstance(function_args, dict):
            function_args = {}
            raw_args_detail = str(raw_args)
            raw_args = "{}"
            function["arguments"] = raw_args
            logger.error(
                f"Non-object JSON args for '{function_name}'. Using {{}}."
            )
            self.metrics.record_anomaly(
                session_key,
                "non_object_tool_args",
                detail=f"{function_name}:{raw_args_detail[:120]}",
            )

        function_name, function_args = self._normalize_tool_alias(
            function_name, function_args, session_key
        )
        function["name"] = function_name
        function["arguments"] = json.dumps(function_args)
        return tc_id, function_name, function_args

    def _sanitize_messages_for_llm(
        self, messages: List[Dict[str, Any]], session_key: str
    ) -> List[Dict[str, Any]]:
        """Repair tool history and assistant arguments before sending upstream."""
        normalized, changed = self._normalize_tool_history_messages(messages)
        if changed:
            # Preserve the caller's list identity.  Some provider adapters and
            # tests intentionally hold a reference to the session history.
            messages[:] = normalized
            if getattr(self, "history", {}).get(session_key) is messages:
                self._mark_dirty(session_key)
        for message in messages:
            if not isinstance(message, dict):
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    tool_call["function"] = {"name": "", "arguments": "{}"}
                    function = tool_call["function"]
                function.setdefault("name", "")
                function.setdefault("arguments", "{}")
                self._parse_tool_call(tool_call, session_key)
        return messages

    async def _publish_tool_intents(
        self,
        tool_calls: List[Dict[str, Any]],
        session_key: str,
        msg: Optional[InboundMessage],
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        for tool_call in tool_calls:
            tc_id, function_name, function_args = self._parse_tool_call(
                tool_call, session_key
            )
            preview = self._build_confirmation_preview(
                function_name, function_args, session_key
            )
            await self._publish_both(
                msg,
                "",
                {
                    "type": "tool_execution",
                    "status": "planned",
                    "tool": function_name,
                    "args": redact_sensitive_value(function_args),
                    "preview": redact_sensitive_value(preview),
                    "tool_call_id": tc_id,
                    "turn_id": turn_id,
                    "message_id": message_id,
                },
            )

    async def _execute_tool_batch(
        self,
        tool_calls: list,
        session_key: str,
        msg: Optional[InboundMessage],
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
        coding_turn: bool = False,
        on_progress_queued: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Run reads concurrently, but keep stateful steps in causal model order."""
        any_blocked = False
        self._last_tool_outcomes = []

        async def _run_one(tool_call: dict):
            tc_id = tool_call["id"]
            function_name = tool_call["function"]["name"]
            is_internal = False

            try:
                current_image_attachments = [
                    dict(attachment)
                    for attachment in (
                        (msg.metadata.get("attachments") or []) if msg else []
                    )
                    if isinstance(attachment, dict)
                    and attachment.get("kind") == "image"
                ]
                recent_image_attachments = self._get_recent_image_attachments(
                    session_key
                )
                tool_context.set(
                    {
                        "tc_id": tc_id,
                        "channel": msg.channel if msg else "system",
                        "sender_id": msg.sender_id if msg else "system",
                        "chat_id": msg.chat_id if msg else "system",
                        "turn_id": turn_id or "",
                        "message_id": message_id or "",
                        "attachments": current_image_attachments,
                        "recent_image_attachments": recent_image_attachments,
                        "auto_reference_images": bool(current_image_attachments)
                        or bool(
                            recent_image_attachments
                            and self._message_refers_to_recent_image(
                                msg.content if msg else ""
                            )
                        ),
                    }
                )

                tc_id, function_name, function_args = self._parse_tool_call(
                    tool_call, session_key
                )

                if coding_turn:
                    await self._emit_coding_phase(
                        self._tool_phase(function_name, function_args),
                        session_key,
                        msg,
                        turn_id=turn_id,
                        message_id=message_id,
                    )

                logger.info(
                    f"Executing: {function_name}({redact_sensitive_text(function_args)})"
                )

                is_internal = False
                is_whatsapp = (
                    msg is not None and getattr(msg, "channel", "") == "whatsapp"
                )

                if function_name == "run_command":
                    validation_error = self.toolbox.validate_command(
                        str(function_args.get("command") or "")
                    )
                    if validation_error:
                        return (
                            tc_id,
                            function_name,
                            function_args,
                            validation_error,
                            False,
                            is_internal,
                        )

                if function_name in _SENSITIVE_TOOLS:
                    approval = self._get_tool_approval_decision(
                        session_key,
                        function_name,
                        is_internal=is_internal,
                        is_whatsapp=is_whatsapp,
                        function_args=function_args,
                    )
                    client_source = str(
                        getattr(msg, "channel", "") or "system"
                    ).strip().lower()
                    if approval["allowed"]:
                        self._log_session_event(
                            session_key,
                            {
                                "type": "approval_decided",
                                "conf_id": f"policy_{uuid.uuid4().hex[:8]}",
                                "tool": function_name,
                                "approved": True,
                                "session_whitelist": approval["reason"] == "session_whitelist",
                                "policy_profile": approval["policy_profile"],
                                "decision_reason": approval["reason"],
                                "client_source": client_source,
                            },
                        )

                    if approval["requires_confirmation"]:
                        conf_id = f"conf_{uuid.uuid4().hex[:8]}"
                        event = asyncio.Event()
                        confirmation_preview = self._build_confirmation_preview(
                            function_name, function_args, session_key
                        )
                        self.pending_confirmations[conf_id] = {
                            "event": event,
                            "approved": False,
                            "session_key": session_key,
                            "tool": function_name,
                            "whitelist_key": self._session_whitelist_key(
                                function_name, function_args
                            ),
                            "preview": confirmation_preview,
                            "policy_profile": approval["policy_profile"],
                            "decision_reason": approval["reason"],
                            "client_source": client_source,
                        }
                        self._log_session_event(
                            session_key,
                            {
                                "type": "approval_requested",
                                "conf_id": conf_id,
                                "tool": function_name,
                                "preview": self._approval_audit_preview(
                                    confirmation_preview
                                ),
                                "policy_profile": approval["policy_profile"],
                                "decision_reason": approval["reason"],
                                "client_source": client_source,
                            },
                        )

                        embed_fields = self._build_confirmation_embed(
                            function_name,
                            function_args,
                            session_key,
                            preview=confirmation_preview,
                        )

                        tool_content = (
                            f"🛠️ Executing {function_name}..." if is_whatsapp else "⏳"
                        )
                        conf_meta = {
                            "type": "tool_execution",
                            "status": "waiting_confirmation",
                            "tool": function_name,
                            "args": function_args,
                            "preview": confirmation_preview,
                            "tool_call_id": tc_id,
                            "conf_id": conf_id,
                            "policy_profile": approval["policy_profile"],
                            "decision_reason": approval["reason"],
                            "turn_id": turn_id,
                            "message_id": message_id,
                            "embed": {
                                "title": "Exec Approval Required",
                                "description": "A command needs your approval.",
                                "color": "#F59E0B",
                                "fields": embed_fields,
                                "footer": f"Expires in 300s | ID: {conf_id}",
                            },
                        }
                        await self._publish(msg, tool_content, conf_meta)
                        if msg and msg.channel != "web":
                            web_meta = {
                                k: v for k, v in conf_meta.items() if k != "embed"
                            }
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel="web",
                                    chat_id=msg.chat_id or "system",
                                    content="",
                                    metadata=web_meta,
                                )
                            )

                        try:
                            await asyncio.wait_for(event.wait(), timeout=300)
                            if not self.pending_confirmations[conf_id]["approved"]:
                                return (
                                    tc_id,
                                    function_name,
                                    function_args,
                                    "ACTION CANCELLED: User denied.",
                                    False,
                                    is_internal,
                                )
                        except asyncio.TimeoutError:
                            self._log_session_event(
                                session_key,
                                {
                                    "type": "approval_timed_out",
                                    "conf_id": conf_id,
                                    "tool": function_name,
                                    "approved": False,
                                    "session_whitelist": False,
                                    "policy_profile": approval["policy_profile"],
                                    "decision_reason": "timeout",
                                    "client_source": client_source,
                                },
                            )
                            return (
                                tc_id,
                                function_name,
                                function_args,
                                "ACTION CANCELLED: Timed out.",
                                False,
                                is_internal,
                            )
                        except asyncio.CancelledError:
                            self._log_session_event(
                                session_key,
                                {
                                    "type": "approval_decided",
                                    "conf_id": conf_id,
                                    "tool": function_name,
                                    "approved": False,
                                    "session_whitelist": False,
                                    "policy_profile": approval["policy_profile"],
                                    "decision_reason": "run_cancelled",
                                    "client_source": client_source,
                                },
                            )
                            raise
                        finally:
                            self.pending_confirmations.pop(conf_id, None)

                tool_content = (
                    f"🛠️ Executing {function_name}..." if is_whatsapp else "⏳"
                )
                run_meta = {
                    "type": "tool_execution",
                    "status": "running",
                    "tool": function_name,
                    "args": redact_sensitive_value(function_args),
                    "tool_call_id": tc_id,
                    "turn_id": turn_id,
                    "message_id": message_id,
                }
                await self._publish_both(msg, tool_content, run_meta)
                if on_progress_queued:
                    on_progress_queued("tool_progress")
                self._log_session_event(
                    session_key,
                    {
                        "type": "tool_started",
                        "tool": function_name,
                        "tool_call_id": tc_id,
                        "args": (
                            {"redacted": True}
                            if function_name in _SENSITIVE_TOOLS
                            else redact_sensitive_value(function_args)
                        ),
                    },
                )

                t0 = time.time()
                try:
                    # Image generation deliberately has no outer LimeBot deadline;
                    # each provider request retains its own transport timeout.
                    tool_timeout = self._tool_execution_timeout(function_name)
                    if tool_timeout and tool_timeout > 0:
                        result = await asyncio.wait_for(
                            self._execute_tool(
                                function_name, function_args, session_key
                            ),
                            timeout=tool_timeout,
                        )
                    else:
                        result = await self._execute_tool(
                            function_name, function_args, session_key
                        )
                except asyncio.TimeoutError:
                    timeout_msg = (
                        f"{int(tool_timeout)}s"
                        if tool_timeout and tool_timeout > 0
                        else "configured limit"
                    )
                    result = (
                        f"Error: Tool '{function_name}' timed out after {timeout_msg}."
                    )
                    logger.error(result)

                self.metrics.record_tool_call(
                    session_key, function_name, time.time() - t0
                )

            except Exception as e:
                result = str(e)
                function_args = locals().get("function_args", {})
                self.metrics.record_tool_call(session_key, function_name, 0, error=True)

            is_blocked = str(result).startswith("ACTION BLOCKED:")
            self._log_session_event(
                session_key,
                {
                    "type": "tool_finished",
                    "tool": function_name,
                    "tool_call_id": tc_id,
                    "blocked": is_blocked,
                    "is_internal": is_internal,
                    "result_preview": str(result)[:400],
                },
            )
            return tc_id, function_name, function_args, result, is_blocked, is_internal

        # A read-only group may run concurrently, but every mutation and shell
        # command forms a barrier. This keeps write -> test causal without
        # weakening the confirmation gate inside _run_one.
        outcomes = []
        read_group = []

        async def _flush_reads() -> None:
            nonlocal read_group
            if read_group:
                outcomes.extend(
                    await asyncio.gather(
                        *[_run_one(call) for call in read_group],
                        return_exceptions=True,
                    )
                )
                read_group = []

        for tool_call in tool_calls:
            function = tool_call.get("function") if isinstance(tool_call, dict) else {}
            function_name = str((function or {}).get("name") or "")
            if function_name in _READ_ONLY_TOOL_NAMES:
                read_group.append(tool_call)
                continue
            await _flush_reads()
            outcomes.append(await _run_one(tool_call))
        await _flush_reads()

        for i, outcome in enumerate(outcomes):
            if isinstance(outcome, Exception):
                logger.error(f"⚠ Tool batch exception: {outcome}")
                # We need to broadcast an error so the UI doesn't get stuck in 'Running'
                # Extract basic info from the original tool_calls list
                fail_tc = tool_calls[i]
                fail_tc_id = fail_tc.get("id", "unknown")
                fail_name = fail_tc.get("function", {}).get("name", "unknown")
                fail_args = {}
                try:
                    fail_args = json.loads(
                        fail_tc.get("function", {}).get("arguments", "{}")
                    )
                except Exception:
                    pass

                try:
                    err_meta = {
                        "type": "tool_execution",
                        "tool": fail_name,
                        "status": "error",
                        "args": redact_sensitive_value(fail_args),
                        "result": redact_sensitive_text(
                            f"Execution failed or was cancelled: {type(outcome).__name__}"
                        ),
                        "tool_call_id": fail_tc_id,
                        "turn_id": turn_id,
                        "message_id": message_id,
                    }
                    await self._publish_both(msg, fail_name, err_meta)
                except Exception:
                    pass
                failure_result = (
                    f"Error executing {fail_name}: {type(outcome).__name__}"
                )
                self._last_tool_outcomes.append(
                    self._build_tool_outcome(fail_name, failure_result)
                )
                self.history[session_key].append(
                    {
                        "role": "tool",
                        "tool_call_id": fail_tc_id,
                        "name": fail_name,
                        "content": failure_result,
                    }
                )
                self._mark_dirty(session_key)
                continue

            tc_id, function_name, function_args, result, is_blocked, is_internal = (
                outcome
            )
            if is_blocked:
                any_blocked = True

            clean_result, tool_images = self._extract_tool_media_payload(result)
            if clean_result:
                result = clean_result

            outcome_details = self._build_tool_outcome(function_name, result)
            self._last_tool_outcomes.append(outcome_details)
            if coding_turn:
                await self._emit_coding_phase(
                    self._tool_phase(function_name, function_args),
                    session_key,
                    msg,
                    turn_id=turn_id,
                    message_id=message_id,
                    outcome=outcome_details,
                )
                await self._refresh_workspace_changeset(
                    session_key,
                    msg,
                    outcome_details,
                    blocked=is_blocked,
                    turn_id=turn_id,
                    message_id=message_id,
                )

            if not is_internal:
                tool_status = (
                    "error"
                    if self._is_tool_result_error(function_name, result)
                    else "completed"
                )
                try:
                    done_meta = {
                        "type": "tool_execution",
                        "tool": function_name,
                        "status": tool_status,
                        "args": redact_sensitive_value(function_args),
                        "result": redact_sensitive_text(result)[
                            :TOOL_BROADCAST_MAX_CHARS
                        ],
                        "tool_call_id": tc_id,
                        "turn_id": turn_id,
                        "message_id": message_id,
                    }
                    await self._publish_both(msg, "", done_meta)
                except Exception:
                    pass

            limit = _TOOL_RESULT_LIMITS.get(function_name, _DEFAULT_TOOL_RESULT_LIMIT)
            str_result = truncate_tool_result(result, limit)

            self.history[session_key].append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": function_name,
                    "content": str_result,
                }
            )
            self._mark_dirty(session_key)

            if tool_images and not is_internal:
                self.history[session_key].append(
                    {
                        "role": "user",
                        "content": self._build_tool_image_followup_content(
                            function_name, tool_images
                        ),
                    }
                )
                self._mark_dirty(session_key)
                self._sessions_pending_tool_image_reply.add(session_key)

        return any_blocked

    async def send_tool_progress(
        self, tool_call_id: str, chat_id: str, content: str
    ) -> None:
        ctx = tool_context.get() or {}
        await self.bus.publish_outbound(
            OutboundMessage(
                channel="web",
                chat_id=chat_id,
                content=content,
                metadata={
                    "type": "tool_execution",
                    "status": "progress",
                    "tool_call_id": tool_call_id,
                    "turn_id": ctx.get("turn_id") or None,
                    "message_id": ctx.get("message_id") or None,
                },
            )
        )

    def _extract_tool_from_content(self, content: str) -> list:
        if not content or not content.strip():
            return []

        default_arg_names = {
            "read_file": "path",
            "edit_file": "edits",
            "verify_files": "paths",
            "diagnose_files": "paths",
            "write_file": "content",
            "create_spreadsheet": "path",
            "calculate": "expression",
            "delete_file": "path",
            "list_dir": "path",
            "search_files": "query",
            "run_command": "command",
            "memory_search": "query",
            "memory_save": "content",
            "generate_image": "prompt",
            "google_search": "query",
            "browser_navigate": "url",
            "spawn_agent": "task",
            "send_media": "path",
            "send_voice": "text",
            "send_discord_message": "message",
            "send_discord_embed": "description",
            "cron_remove": "job_id",
            "save_memory": "content",
            "log_memory": "content",
        }

        def _make_tool_call(function_name: str, function_args: Dict[str, Any]) -> list:
            return [
                {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "arguments": json.dumps(function_args),
                    },
                }
            ]

        def _tool_code_call(tool_expr: str) -> list:
            raw_expr = str(tool_expr or "").strip()
            if not raw_expr:
                return []

            try:
                parsed = ast.parse(raw_expr, mode="eval")
            except Exception:
                return []

            call = parsed.body
            if not isinstance(call, ast.Call):
                return []
            if not isinstance(call.func, ast.Name):
                return []

            canonical_name = TOOL_NAME_ALIASES.get(call.func.id, call.func.id)
            arg_names = {
                "read_file": ["path"],
                "edit_file": ["path", "edits", "expected_sha256"],
                "verify_files": ["paths", "include_diagnostics", "provider"],
                "diagnose_files": ["paths", "provider", "timeout"],
                "write_file": ["path", "content"],
                "create_spreadsheet": ["path", "sheets", "title"],
                "calculate": ["expression"],
                "delete_file": ["path"],
                "list_dir": ["path"],
                "search_files": ["query", "path"],
                "run_command": ["command"],
                "memory_search": ["query"],
                "memory_save": ["content", "scope"],
                "google_search": ["query"],
                "browser_navigate": ["url"],
                "spawn_agent": ["task"],
                "cron_remove": ["job_id"],
                "save_memory": ["content"],
                "log_memory": ["content"],
            }.get(canonical_name)
            if arg_names is None:
                return []

            function_args: Dict[str, Any] = {}
            try:
                for idx, arg in enumerate(call.args):
                    if idx >= len(arg_names):
                        return []
                    function_args[arg_names[idx]] = ast.literal_eval(arg)
                for kw in call.keywords:
                    if kw.arg is None:
                        return []
                    function_args[kw.arg] = ast.literal_eval(kw.value)
            except Exception:
                return []

            return _make_tool_call(canonical_name, function_args)

        def _legacy_xml_tool_call(tag_name: str, inner_content: str) -> list:
            canonical_name = TOOL_NAME_ALIASES.get(tag_name, tag_name)
            raw_content = (inner_content or "").strip()
            if not raw_content:
                return []

            function_args: Dict[str, Any]
            if raw_content.startswith("{") and raw_content.endswith("}"):
                try:
                    parsed = json.loads(raw_content)
                    if isinstance(parsed, dict):
                        function_args = parsed
                    else:
                        function_args = {}
                except Exception:
                    function_args = {}
            else:
                default_arg_name = default_arg_names.get(canonical_name)
                if not default_arg_name:
                    return []
                function_args = {default_arg_name: raw_content}

            return _make_tool_call(canonical_name, function_args)

        def _xml_attr_tool_call(tag_name: str, raw_attrs: str) -> list:
            canonical_name = TOOL_NAME_ALIASES.get(tag_name, tag_name)
            default_arg_name = default_arg_names.get(canonical_name)
            if not default_arg_name:
                return []

            attrs: Dict[str, Any] = {}
            for match in re.finditer(
                r'([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', raw_attrs or ""
            ):
                attrs[match.group(1)] = (
                    match.group(2) if match.group(2) is not None else match.group(3)
                )

            if not attrs:
                return []

            if default_arg_name in attrs:
                function_args = {default_arg_name: attrs[default_arg_name]}
            else:
                first_value = next(iter(attrs.values()))
                function_args = {default_arg_name: first_value}
            return _make_tool_call(canonical_name, function_args)

        def _implicit_tool_call_from_dict(parsed: dict):
            if not isinstance(parsed, dict) or "name" in parsed:
                return []

            if isinstance(parsed.get("prompt"), str) and parsed["prompt"].strip():
                image_keys = {
                    "model",
                    "size",
                    "quality",
                    "count",
                    "reference_images",
                    "use_attached_images",
                }
                if image_keys.intersection(parsed.keys()):
                    return _make_tool_call(
                        "generate_image",
                        {
                            key: value
                            for key, value in parsed.items()
                            if key
                            in {
                                "prompt",
                                "model",
                                "size",
                                "quality",
                                "count",
                                "reference_images",
                                "use_attached_images",
                            }
                        },
                    )

            if isinstance(parsed.get("url"), str) and parsed["url"].strip():
                return _make_tool_call("browser_navigate", {"url": parsed["url"]})

            if isinstance(parsed.get("query"), str) and parsed["query"].strip():
                return _make_tool_call("google_search", {"query": parsed["query"]})

            cmd = parsed.get("cmd")
            if isinstance(cmd, list):
                cmd_text = " ".join(str(part or "") for part in cmd).strip().lower()
                if re.search(r"\b(ls|dir)\b", cmd_text):
                    return _make_tool_call("list_dir", {"path": "."})

            return []

        try:
            cleaned = content
            if "```" in content:
                m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
                if m:
                    cleaned = m.group(1)

            s = cleaned.find("{")
            e = cleaned.rfind("}")
            if s != -1 and e >= s:
                json_str = cleaned[s : e + 1]
                parsed = json.loads(json_str)
                lower = json_str.lower()
                if isinstance(parsed, dict) and "name" in parsed and '"name"' in lower and (
                    '"arguments"' in lower or '"parameters"' in lower
                ):
                    args = parsed.get("arguments", parsed.get("parameters", {}))
                    args_str = (
                        json.dumps(args) if isinstance(args, dict) else str(args)
                    )
                    return [
                        {
                            "id": f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": parsed["name"],
                                "arguments": args_str,
                            },
                        }
                    ]
                implicit = _implicit_tool_call_from_dict(parsed)
                if implicit:
                    return implicit
        except Exception:
            pass

        tool_calls = []
        try:
            pattern = (
                r"<\|tool_call_begin\|>\s*(?:functions\.)?([\w\.]+)(?::\d+)?\s*"
                r"<\|tool_call_argument_begin\|>\s*({.*?})\s*<\|tool_call_end\|>"
            )
            for m in re.finditer(pattern, content, re.DOTALL):
                tool_calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": m.group(1), "arguments": m.group(2)},
                    }
                )
        except Exception:
            pass
        if tool_calls:
            return tool_calls

        # Some OpenAI-compatible bridges serialize a tool call using the
        # provider-facing ``to=functions.name code: {...}`` envelope. Recover
        # it instead of leaking the protocol text into the visible reply.
        try:
            provider_envelope = _VISIBLE_PROVIDER_TOOL_CALL_PREFIX_RE.search(content)
            if provider_envelope:
                raw_tail = content[provider_envelope.end() :].lstrip()
                parsed_args, _ = json.JSONDecoder().raw_decode(raw_tail)
                if isinstance(parsed_args, dict):
                    return _make_tool_call(
                        provider_envelope.group("name"), parsed_args
                    )
        except Exception:
            pass

        try:
            pipe_tag_pattern = re.compile(
                r"<\|(?P<tag>[A-Za-z_][\w]*)\|>\s*(?P<body>{.*?})\s*<\|/(?P=tag)\|>",
                re.DOTALL,
            )
            for match in pipe_tag_pattern.finditer(content):
                extracted = _legacy_xml_tool_call(
                    match.group("tag"), match.group("body")
                )
                if extracted:
                    return extracted

            tool_code_pattern = re.compile(
                r"<tool_code>\s*(?P<body>.*?)\s*</tool_code>",
                re.IGNORECASE | re.DOTALL,
            )
            for match in tool_code_pattern.finditer(content):
                extracted = _tool_code_call(match.group("body"))
                if extracted:
                    return extracted

            legacy_tag_pattern = re.compile(
                r"<(?P<tag>[A-Za-z_][\w]*)>\s*(?P<body>.*?)\s*</(?P=tag)>",
                re.DOTALL,
            )
            for match in legacy_tag_pattern.finditer(content):
                extracted = _legacy_xml_tool_call(
                    match.group("tag"), match.group("body")
                )
                if extracted:
                    return extracted

            legacy_attr_tag_pattern = re.compile(
                r"<(?P<tag>[A-Za-z_][\w]*)\s+(?P<attrs>[^<>]*?)\s*/?>",
                re.DOTALL,
            )
            for match in legacy_attr_tag_pattern.finditer(content):
                extracted = _xml_attr_tool_call(
                    match.group("tag"), match.group("attrs")
                )
                if extracted:
                    return extracted

            bare_call_pattern = re.compile(
                r"\b(?:list_dir|read_file|edit_file|write_file|delete_file|search_files|verify_files|diagnose_files|"
                r"run_command|memory_search|memory_save|google_search|browser_navigate|"
                r"spawn_agent|send_media|send_voice|generate_image|send_discord_message|send_discord_embed|list_discord_channels|cron_remove|save_memory|log_memory|ls|dir|cat|"
                r"grep|rg|ripgrep|find_files|shell|terminal|exec|bash|"
                r"powershell|cmd)\s*\([^)]*\)"
            )
            for match in bare_call_pattern.finditer(content):
                extracted = _tool_code_call(match.group(0))
                if extracted:
                    return extracted
        except Exception:
            pass

        # ── Kimi K2 inline format: functions.tool_name:N{"arg":"val"} ──
        try:
            kimi_pattern = r"(?:^|\s)(?::?functions\.)(\w+)(?::\d+)?\s*(\{[^}]*\})"
            for m in re.finditer(kimi_pattern, content, re.DOTALL):
                try:
                    args = json.loads(m.group(2))
                except json.JSONDecodeError:
                    continue
                tool_calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": m.group(1),
                            "arguments": json.dumps(args),
                        },
                    }
                )
        except Exception:
            pass

        return tool_calls

    @staticmethod
    def _trim_leading_structural_lines(content: str) -> str:
        """Drop leading fence/bracket residue that sometimes leaks before tool calls."""
        if not content:
            return ""

        lines = content.splitlines(keepends=True)
        while lines:
            stripped = lines[0].strip()
            if not stripped:
                lines.pop(0)
                continue
            if re.fullmatch(r"[`{}\[\],:;]+", stripped):
                lines.pop(0)
                continue
            break
        return "".join(lines)

    def _sanitize_tool_call_content(self, content: str) -> str:
        """Keep only meaningful prose when the model mixes text with tool syntax."""
        if not content:
            return ""

        cleaned = content
        marker_positions = []
        legacy_tag_pattern = (
            r"<(?:read_file|edit_file|write_file|delete_file|list_dir|search_files|verify_files|diagnose_files|run_command|"
            r"memory_search|memory_save|google_search|browser_navigate|spawn_agent|send_media|send_voice|generate_image|send_discord_message|send_discord_embed|list_discord_channels|"
            r"save_memory|log_memory|ls|dir|list_files|cat|open_file|show_file|"
            r"grep|rg|ripgrep|find_files|shell|terminal|exec|bash|powershell|cmd)>"
        )
        tool_code_pattern = r"<tool_code>"

        inline_match = re.search(r"(?::?functions\.\w+(?::\d+)?\s*\{)", cleaned)
        if inline_match:
            marker_positions.append(inline_match.start())

        visible_provider_match = _VISIBLE_PROVIDER_TOOL_CALL_PREFIX_RE.search(cleaned)
        if visible_provider_match:
            marker_positions.append(visible_provider_match.start())

        block_match = re.search(r"<\|tool_call_begin\|>", cleaned)
        if block_match:
            marker_positions.append(block_match.start())

        pipe_tag_match = re.search(r"<\|[A-Za-z_][\w]*\|>", cleaned)
        if pipe_tag_match:
            marker_positions.append(pipe_tag_match.start())

        json_tool_match = re.search(
            r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"(?:arguments|parameters)"\s*:\s*\{',
            cleaned,
            flags=re.DOTALL,
        )
        if json_tool_match:
            marker_positions.append(json_tool_match.start())

        image_args_match = re.search(
            r'\{\s*"prompt"\s*:\s*".*?"\s*,\s*"(?:model|size|quality|count)"\s*:',
            cleaned,
            flags=re.DOTALL,
        )
        if image_args_match:
            marker_positions.append(image_args_match.start())

        legacy_tag_match = re.search(legacy_tag_pattern, cleaned, flags=re.IGNORECASE)
        if legacy_tag_match:
            marker_positions.append(legacy_tag_match.start())
        tool_code_match = re.search(tool_code_pattern, cleaned, flags=re.IGNORECASE)
        if tool_code_match:
            marker_positions.append(tool_code_match.start())

        if marker_positions:
            cleaned = cleaned[: min(marker_positions)]

        cleaned = re.sub(
            r"```(?:json)?\s*\{.*?\}\s*```",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        cleaned = re.sub(
            r"<\|tool_call_begin\|>.*?<\|tool_call_end\|>",
            "",
            cleaned,
            flags=re.DOTALL,
        )
        cleaned = re.sub(
            r"<\|[A-Za-z_][\w]*\|>.*?<\|/[A-Za-z_][\w]*\|>",
            "",
            cleaned,
            flags=re.DOTALL,
        )
        cleaned = re.sub(
            r"(?::?functions\.\w+(?::\d+)?\s*\{[^}]*\})",
            "",
            cleaned,
        )
        cleaned = re.sub(
            legacy_tag_pattern + r".*?</(?:read_file|edit_file|write_file|delete_file|list_dir|search_files|verify_files|diagnose_files|run_command|memory_search|memory_save|google_search|browser_navigate|spawn_agent|send_media|send_voice|generate_image|send_discord_message|send_discord_embed|list_discord_channels|save_memory|log_memory|ls|dir|list_files|cat|open_file|show_file|grep|rg|ripgrep|find_files|shell|terminal|exec|bash|powershell|cmd)>",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        cleaned = re.sub(
            r"<tool_code>.*?</tool_code>",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        cleaned = re.sub(
            r'^\s*\{\s*"path"\s*:\s*".*?"\s*\}\s*$',
            "",
            cleaned,
            flags=re.MULTILINE,
        )
        # Strip bare JSON tool-call objects: {"name": "...", "arguments|parameters": {...}}
        cleaned = re.sub(
            r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"(?:arguments|parameters)"\s*:\s*\{[^}]*\}\s*\}',
            "",
            cleaned,
        )
        cleaned = re.sub(
            r'\{\s*"prompt"\s*:\s*".*?"\s*,\s*"(?:model|size|quality|count)"\s*:.*?\}\s*$',
            "",
            cleaned,
            flags=re.DOTALL,
        )
        cleaned = self._trim_leading_structural_lines(cleaned)
        cleaned_lines = []
        for line in cleaned.splitlines():
            stripped = line.strip()
            if stripped and re.fullmatch(r"[`{}\[\],:;]+", stripped):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("prompt"), str) and {
                    "model",
                    "size",
                    "quality",
                    "count",
                }.intersection(parsed.keys()):
                    return ""
                if isinstance(parsed.get("url"), str) and parsed["url"].strip():
                    return ""
                if isinstance(parsed.get("query"), str) and parsed["query"].strip():
                    return ""
        except Exception:
            pass

        if not re.search(r"[A-Za-z0-9]", cleaned):
            return ""
        return cleaned

    async def _consume_stream(
        self,
        response_stream,
        msg: InboundMessage,
        session_key: str,
        previous_content: str = "",
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
        turn_started_at: Optional[float] = None,
        llm_started_at: Optional[float] = None,
        iteration_kind: str = "initial",
        iteration: int = 0,
        on_output_queued: Optional[Callable[[str], None]] = None,
    ):
        full_content: str = ""
        tool_calls: list = []
        usage = None
        is_potential_json: bool = False
        streamed_any: bool = False
        display_buffer: str = ""
        ghost_active: Optional[str] = None
        match_index = 0
        dedup_active = bool(previous_content)
        streamed_to_web: bool = False
        streamed_to_discord: bool = False
        last_flush = time.monotonic()
        flush_interval_s = 0.08
        flush_min_chars = 256
        thinking_buffer: str = ""
        extracted_from = "provider_tool_calls"
        chunk_index = 0
        provider_first_delta_recorded = False

        def first_delta_metadata(delta_kind: str) -> Dict[str, Any]:
            return {
                "iteration_kind": iteration_kind,
                "iteration": iteration,
                "delta_kind": delta_kind,
            }

        def record_provider_first_delta(delta_kind: str) -> None:
            nonlocal provider_first_delta_recorded
            if provider_first_delta_recorded or llm_started_at is None:
                return
            provider_first_delta_recorded = True
            self._record_stage_timing(
                session_key,
                "provider_first_delta",
                llm_started_at,
                metadata=first_delta_metadata(delta_kind),
            )

        try:
            async for chunk in response_stream:
                chunk_index += 1
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage

                delta = chunk.choices[0].delta

                if hasattr(delta, "content") and delta.content:
                    record_provider_first_delta("content")
                    content_chunk = delta.content
                    self._log_tool_debug(
                        "stream_chunk_content",
                        session_key=session_key,
                        chunk_index=chunk_index,
                        delta_content=content_chunk,
                    )

                    if full_content and content_chunk == full_content:
                        continue
                    if len(content_chunk) > 5 and full_content.startswith(
                        content_chunk
                    ):
                        continue
                    if full_content and content_chunk.startswith(full_content):
                        content_chunk = content_chunk[len(full_content) :]

                    to_stream = content_chunk
                    if dedup_active:
                        remaining_prev = previous_content[match_index:]
                        if not remaining_prev:
                            dedup_active = False
                        else:
                            common = 0
                            for i in range(
                                min(len(content_chunk), len(remaining_prev))
                            ):
                                if content_chunk[i] == remaining_prev[i]:
                                    common += 1
                                else:
                                    break
                            if common > 0:
                                match_index += common
                                to_stream = content_chunk[common:]
                                if common < len(content_chunk):
                                    dedup_active = False
                            else:
                                dedup_active = False

                    full_content += content_chunk

                    if not streamed_any:
                        stripped = full_content.strip()
                        if not stripped:
                            continue
                        is_potential_json = stripped.startswith(
                            "{"
                        ) or stripped.startswith("```")
                        streamed_any = True

                    if msg.channel == "web" and not is_potential_json and to_stream:
                        display_buffer += to_stream
                        display_buffer = self._trim_leading_structural_lines(
                            display_buffer
                        )
                        if not display_buffer.strip():
                            continue

                        now = time.monotonic()
                        should_flush = (
                            len(display_buffer) >= flush_min_chars
                            or (now - last_flush) >= flush_interval_s
                        )

                        while display_buffer and should_flush:
                            if not ghost_active:
                                tag_start = display_buffer.find("<")
                                if tag_start == -1:
                                    await self.bus.publish_outbound(
                                        OutboundMessage(
                                            channel=msg.channel,
                                            chat_id=msg.chat_id,
                                            content=display_buffer,
                                            metadata=self._with_trace_metadata(
                                                {"type": "chunk"},
                                                turn_id=turn_id,
                                                message_id=message_id,
                                            ),
                                        )
                                    )
                                    if on_output_queued:
                                        on_output_queued("content")
                                    display_buffer = ""
                                    last_flush = time.monotonic()
                                    break

                                if tag_start > 0:
                                    await self.bus.publish_outbound(
                                        OutboundMessage(
                                            channel=msg.channel,
                                            chat_id=msg.chat_id,
                                            content=display_buffer[:tag_start],
                                            metadata=self._with_trace_metadata(
                                                {"type": "chunk"},
                                                turn_id=turn_id,
                                                message_id=message_id,
                                            ),
                                        )
                                    )
                                    if on_output_queued:
                                        on_output_queued("content")
                                    streamed_to_web = True
                                    last_flush = time.monotonic()
                                    display_buffer = display_buffer[tag_start:]

                                tag_end = display_buffer.find(">")
                                if tag_end == -1:
                                    break

                                tag_content = display_buffer[: tag_end + 1]
                                found_ghost = None
                                if not tag_content.startswith("</"):
                                    for g in _GHOST_TAG_NAMES:
                                        if tag_content.startswith(f"<{g}"):
                                            found_ghost = g
                                            break

                                if found_ghost:
                                    ghost_active = found_ghost
                                    await self.bus.publish_outbound(
                                        OutboundMessage(
                                            channel=msg.channel,
                                            chat_id=msg.chat_id,
                                            content="",
                                            metadata={
                                                "type": "activity",
                                                "text": f"🧠 Processing {found_ghost}…",
                                            },
                                        )
                                    )
                                    display_buffer = display_buffer[tag_end + 1 :]
                                elif tag_content.startswith("</"):
                                    display_buffer = display_buffer[tag_end + 1 :]
                                else:
                                    await self.bus.publish_outbound(
                                        OutboundMessage(
                                            channel=msg.channel,
                                            chat_id=msg.chat_id,
                                            content=tag_content,
                                            metadata=self._with_trace_metadata(
                                                {"type": "chunk"},
                                                turn_id=turn_id,
                                                message_id=message_id,
                                            ),
                                        )
                                    )
                                    if on_output_queued:
                                        on_output_queued("content")
                                    streamed_to_web = True
                                    display_buffer = display_buffer[tag_end + 1 :]
                            else:
                                closing = f"</{ghost_active}>"
                                close_idx = display_buffer.find(closing)
                                if close_idx == -1:
                                    display_buffer = ""
                                    break
                                ghost_active = None
                                await self.bus.publish_outbound(
                                    OutboundMessage(
                                        channel=msg.channel,
                                        chat_id=msg.chat_id,
                                        content="",
                                        metadata={"type": "activity", "text": ""},
                                    )
                                )
                                display_buffer = display_buffer[
                                    close_idx + len(closing) :
                                ]

                # Discord intentionally buffers provider deltas. Its typing
                # indicator stays active and the normal delivery path sends
                # only the completed response. Web remains token-streamed.
                thinking = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "thinking", None
                )
                if thinking:
                    thinking_buffer += thinking
                    self._log_tool_debug(
                        "stream_chunk_thinking",
                        session_key=session_key,
                        chunk_index=chunk_index,
                        delta_thinking=thinking,
                    )
                if thinking and msg.channel == "web":
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=thinking,
                            metadata=self._with_trace_metadata(
                                {"type": "thinking"},
                                turn_id=turn_id,
                                message_id=message_id,
                            ),
                        )
                    )

                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    record_provider_first_delta("tool_call")
                    self._log_tool_debug(
                        "stream_chunk_tool_delta",
                        session_key=session_key,
                        chunk_index=chunk_index,
                        tool_delta=str(delta.tool_calls),
                    )
                    for tc_chunk in delta.tool_calls:
                        while len(tool_calls) <= tc_chunk.index:
                            tool_calls.append(
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            )
                        tc = tool_calls[tc_chunk.index]
                        if tc_chunk.id:
                            tc["id"] = tc_chunk.id
                        if tc_chunk.function.name:
                            tc["function"]["name"] += tc_chunk.function.name
                        if tc_chunk.function.arguments:
                            tc["function"]["arguments"] += tc_chunk.function.arguments

        except (RateLimitError, Exception) as e:
            err_msg_lower = str(e).lower()
            if (
                "429" in err_msg_lower
                or "RateLimitError" in type(e).__name__
                or "rate limit" in err_msg_lower
                or "rate_limit" in err_msg_lower
                or "usage_limit" in err_msg_lower
                or "usage limit" in err_msg_lower
                or "limit has been reached" in err_msg_lower
                or "limit_reached" in err_msg_lower
                or "codex provider returned an error" in err_msg_lower
                or "no visible response" in err_msg_lower
            ):
                logger.warning(f"⚠ Rate limit during streaming: {e}")
                rate_limit_notice = "\n\n[⚠ Generation stopped due to rate limit/usage limit reached]"
                display_buffer += rate_limit_notice
                full_content += rate_limit_notice
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata=self._with_trace_metadata(
                            {"type": "rate_limit_error", "details": str(e)},
                            turn_id=turn_id,
                            message_id=message_id,
                        ),
                    )
                )
            else:
                raise

        if usage:
            await self.session_manager.update_session(
                session_key=session_key,
                model=self.model,
                origin=msg.channel,
                usage=usage,
            )

        if display_buffer and msg.channel == "web":
            # ── Scrub any ghost / orphan tags that survived streaming ──
            if ghost_active:
                # Stream ended mid-ghost — drop everything up to (and
                # including) the closing tag, or the whole buffer if
                # the closing tag never arrived.
                closing = f"</{ghost_active}>"
                close_idx = display_buffer.find(closing)
                if close_idx != -1:
                    display_buffer = display_buffer[close_idx + len(closing) :]
                else:
                    display_buffer = ""
                ghost_active = None

            display_buffer = self._trim_leading_structural_lines(display_buffer)

            # Strip any leftover opening or closing ghost tags.
            display_buffer = _GHOST_TAG_RE.sub("", display_buffer).strip()

            if display_buffer:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=display_buffer,
                        metadata=self._with_trace_metadata(
                            {"type": "chunk"},
                            turn_id=turn_id,
                            message_id=message_id,
                        ),
                    )
                )
                if on_output_queued:
                    on_output_queued("content")
                streamed_to_web = True
            display_buffer = ""

        if not tool_calls and full_content:
            extracted = self._extract_tool_from_content(full_content)
            if extracted:
                extracted_from = "assistant_content"
                tool_calls = extracted
                clean_content = re.sub(
                    r"```(?:json)?\s*\{.*?\}(?:\s*```)?",
                    "",
                    full_content,
                    flags=re.DOTALL,
                ).strip()
                # Also strip Kimi K2 inline tool syntax: functions.name:N{...}
                clean_content = re.sub(
                    r"(?::?functions\.\w+(?::\d+)?\s*\{[^}]*\})",
                    "",
                    clean_content,
                ).strip()
                clean_content = re.sub(
                    r"<(?:read_file|edit_file|write_file|delete_file|list_dir|search_files|verify_files|diagnose_files|run_command|memory_search|memory_save|google_search|browser_navigate|spawn_agent|send_media|send_voice|generate_image|send_discord_message|send_discord_embed|list_discord_channels|save_memory|log_memory|ls|dir|list_files|cat|open_file|show_file|grep|rg|ripgrep|find_files|shell|terminal|exec|bash|powershell|cmd)>.*?</(?:read_file|edit_file|write_file|delete_file|list_dir|search_files|verify_files|diagnose_files|run_command|memory_search|memory_save|google_search|browser_navigate|spawn_agent|send_media|send_voice|generate_image|send_discord_message|send_discord_embed|list_discord_channels|save_memory|log_memory|ls|dir|list_files|cat|open_file|show_file|grep|rg|ripgrep|find_files|shell|terminal|exec|bash|powershell|cmd)>",
                    "",
                    clean_content,
                    flags=re.DOTALL | re.IGNORECASE,
                ).strip()
                clean_content = re.sub(
                    r"<tool_code>.*?</tool_code>",
                    "",
                    clean_content,
                    flags=re.DOTALL | re.IGNORECASE,
                ).strip()
                if clean_content:
                    full_content = clean_content
                logger.info(f"✨ Extracted tool: {tool_calls[0]['function']['name']}")

        if not tool_calls and thinking_buffer:
            extracted = self._extract_tool_from_content(thinking_buffer)
            if extracted:
                extracted_from = "reasoning_content"
                tool_calls = extracted
                logger.info(
                    f"✨ Extracted tool from reasoning: {tool_calls[0]['function']['name']}"
                )

        valid_tcs = []
        for tc in tool_calls:
            if not tc.get("id"):
                tc["id"] = f"call_{uuid.uuid4().hex[:8]}"
            if not tc.get("type"):
                tc["type"] = "function"

            fn = tc.get("function", {})
            if fn.get("name"):
                # Fix common LLM hallucination: double JSON objects in arguments
                args = fn.get("arguments", "")
                if args.startswith("{") and "}{" in args:
                    try:
                        idx = args.find("}{")
                        json.loads(args[: idx + 1])
                        fn["arguments"] = args[: idx + 1]
                    except Exception:
                        pass
                valid_tcs.append(tc)
        tool_calls = valid_tcs

        if tool_calls and full_content:
            pre_sanitize = full_content
            full_content = self._sanitize_tool_call_content(full_content)
            if pre_sanitize.strip() and pre_sanitize != full_content:
                self.metrics.record_anomaly(
                    session_key,
                    "tool_call_residue_stripped",
                    detail=pre_sanitize[:160],
                )
                self._log_tool_debug(
                    "tool_residue_stripped",
                    session_key=session_key,
                    before=pre_sanitize,
                    after=full_content,
                )
                if msg.channel == "web" and streamed_to_web:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=full_content,
                            metadata=self._with_trace_metadata(
                                {"type": "full_content"},
                                turn_id=turn_id,
                                message_id=message_id,
                            ),
                        )
                    )

        if (
            is_potential_json
            and not tool_calls
            and full_content.strip()
            and msg.channel == "web"
            and not streamed_to_web
        ):
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=full_content,
                    metadata=self._with_trace_metadata(
                        {"type": "full_content"},
                        turn_id=turn_id,
                        message_id=message_id,
                    ),
                )
            )

        self._log_tool_debug(
            "stream_summary",
            session_key=session_key,
            raw_content=full_content,
            thinking=thinking_buffer,
            tool_calls=self._tool_call_debug_rows(tool_calls),
            extracted_from=extracted_from if tool_calls else "none",
            streamed_to_web=streamed_to_web,
            streamed_to_discord=streamed_to_discord,
            usage=str(usage) if usage else "",
        )

        return full_content, tool_calls, usage, streamed_to_web, streamed_to_discord

    async def _process_message(
        self, msg: InboundMessage, _task_id: Optional[str] = None
    ) -> None:
        session_key = msg.session_key
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        assistant_message_id = f"msg_{uuid.uuid4().hex[:12]}"
        turn_started = time.perf_counter()
        tool_batch_duration_s = 0.0
        tool_batch_count = 0
        tool_batch_blocked = False
        unresolved_tool_failure = False
        unresolved_failure_detail = ""

        def make_output_queued_recorder(
            iteration_kind: str, iteration: int
        ) -> Callable[[str], None]:
            recorded = False

            def record(output_kind: str) -> None:
                nonlocal recorded
                if recorded:
                    return
                recorded = True
                self._record_stage_timing(
                    session_key,
                    "turn_first_output_queued",
                    turn_started,
                    metadata={
                        "iteration_kind": iteration_kind,
                        "iteration": iteration,
                        "delta_kind": output_kind,
                    },
                )

            return record

        # ── Task tracking ────────────────────────────────────────────────
        from core.task_tracker import get_task_tracker
        _tracker = get_task_tracker()
        _msg_task_id = _task_id
        task_context_token = (
            _CURRENT_TASK_ID.set(_task_id) if _task_id else None
        )

        try:
            if _msg_task_id:
                await _tracker.update_task(
                    _msg_task_id,
                    status="running",
                    metadata_update={
                        "turn_id": turn_id,
                        "sender_id": msg.sender_id,
                    },
                )
            else:
                _msg_task_id = await _tracker.create_task(
                    task_type="inbound_message",
                    summary=f"{msg.channel}: {(msg.content or '')[:80]}",
                    channel=msg.channel,
                    session_key=session_key,
                    chat_id=msg.chat_id,
                    metadata={"turn_id": turn_id, "sender_id": msg.sender_id},
                )
                task_context_token = _CURRENT_TASK_ID.set(_msg_task_id)
                await _tracker.update_task(_msg_task_id, status="running")

            content = msg.content or ""
            attachments = [
                attachment
                for attachment in (msg.metadata.get("attachments") or [])
                if isinstance(attachment, dict)
            ]
            self._remember_image_attachments(session_key, attachments)
            self._log_session_event(
                session_key,
                {
                    "type": "inbound_message",
                    "turn_id": turn_id,
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "sender_id": msg.sender_id,
                    "is_scheduler": bool(msg.metadata.get("is_scheduler")),
                    "content_preview": content[:500],
                    "attachments": [
                        str(attachment.get("name") or "attachment")
                        for attachment in attachments
                    ],
                },
            )
            if content.startswith("[SCHEDULER] "):
                content = content.replace("[SCHEDULER] ", "").strip()

            if content == "@reflect_and_distill":
                from core.reflection import get_reflection_service

                svc = get_reflection_service(self.bus, self.model)
                reply = await svc.run_reflection_cycle()
                try:
                    tmp = Path("temp_reflect_log.txt")
                    if tmp.exists():
                        tmp.unlink()
                except Exception as e:
                    logger.warning(f"Cleanup error: {e}")

                await process_tags(
                    raw_reply=reply,
                    sender_id=msg.sender_id,
                    validate_soul=prompt_module.validate_and_save_soul,
                    validate_identity=prompt_module.validate_and_save_identity,
                    validate_mood=prompt_module.validate_and_save_mood,
                    validate_relationship=prompt_module.validate_and_save_relationships,
                    vector_service=self.vector_service,
                    bus=self.bus,
                    msg=msg,
                    config=self.config,
                )
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel="web",
                        chat_id="global",
                        content="✨ Background reflection complete.",
                        metadata=self._with_trace_metadata(
                            {"type": "maintenance", "is_maintenance": True},
                            turn_id=turn_id,
                        ),
                    )
                )
                return

            if (
                not msg.metadata.get("mentioned")
                and not msg.metadata.get("is_dm")
                and msg.channel == "discord"
            ):
                return

            if not msg.metadata.get("is_scheduler"):
                if not self.get_readiness_status()["ready"]:
                    await self._publish_activity(
                        msg,
                        "Preparing skills and tools...",
                        turn_id=turn_id,
                        message_id=assistant_message_id,
                    )
                readiness = await self.await_ready()
                if not readiness["ready"]:
                    failure_code = readiness.get("failure_code") or "agent_not_ready"
                    if _msg_task_id:
                        await _tracker.complete_task(
                            _msg_task_id, error=failure_code
                        )
                    reply = (
                        "LimeBot is still preparing its skills and tools. Please retry in a moment."
                        if readiness["status"] == "timeout"
                        else "LimeBot could not load its required capabilities. Run `limebot doctor` and check the startup logs."
                    )
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=reply,
                            metadata=self._with_trace_metadata(
                                {
                                    "is_error": True,
                                    "reply_to": msg.sender_id,
                                    "error_code": failure_code,
                                },
                                turn_id=turn_id,
                                message_id=assistant_message_id,
                            ),
                        )
                    )
                    return

            sender_id = msg.sender_id
            metadata_skill_name = str(msg.metadata.get("skill_name") or "").strip()
            if metadata_skill_name:
                content, forced_skill_name, skill_error = self._resolve_requested_skill(
                    metadata_skill_name,
                    content,
                    raw_content=content,
                )
            else:
                content, forced_skill_name, skill_error = self._resolve_skill_invocation(
                    content
                )
            if skill_error:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=skill_error,
                        metadata=self._with_trace_metadata(
                            {"reply_to": msg.sender_id, "type": "warning"},
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        ),
                    )
                )
                return
            normalized = content.strip().lower()
            is_stop_request = normalized in _DENY_WORDS or any(
                normalized.startswith(k + " ") for k in _DENY_WORDS
            )

            dedup_source = None
            if msg.content:
                message_id = (
                    str(msg.metadata.get("message_id") or "").strip()
                    if msg.metadata
                    else ""
                )
                if message_id:
                    dedup_source = f"message:{message_id}"
                else:
                    dedup_source = f"sender:{msg.sender_id}\ncontent:{msg.content}"

            msg_hash = hash(dedup_source) if dedup_source else None
            if msg_hash is not None and not is_stop_request:
                now_ts = asyncio.get_running_loop().time()
                last_seen = self._last_msg_hash.get(session_key)
                if last_seen is not None:
                    last_hash, last_ts = last_seen
                    if msg_hash == last_hash and (now_ts - last_ts) <= 2.0:
                        if (
                            session_key in self.session_locks
                            and self.session_locks[session_key].locked()
                        ):
                            logger.warning(
                                f"♻️ Message '{msg.content[:20]}...' skipped - session {session_key} is currently BUSY processing another task."
                            )
                        else:
                            logger.debug("♻️ Skipping identical duplicate message.")
                        return
                self._last_msg_hash[session_key] = (msg_hash, now_ts)

            # ── Confirmation intercept (WhatsApp / Discord) ──────────────────
            # The web UI resolves confirmations via a REST button click.
            # On other channels the user types a reply — intercept it here
            # before it enters the full agent loop so the asyncio.Event fires
            # and the waiting tool call is unblocked immediately.
            if msg.channel != "web":
                if is_stop_request:
                    cancelled = await self.cancel_session(session_key)
                    if cancelled:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="🛑 Stopped the current run.",
                                metadata=self._with_trace_metadata(
                                    {"reply_to": msg.sender_id},
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                ),
                            )
                        )
                        return

                pending_for_session = [
                    (cid, c)
                    for cid, c in self.pending_confirmations.items()
                    if c["session_key"] == session_key
                ]
                if pending_for_session:
                    is_approve = normalized in _APPROVE_WORDS or any(
                        normalized.startswith(k + " ") for k in _APPROVE_WORDS
                    )
                    is_deny = normalized in _DENY_WORDS or any(
                        normalized.startswith(k + " ") for k in _DENY_WORDS
                    )
                    if is_approve or is_deny:
                        for conf_id, _ in pending_for_session:
                            await self.confirm_tool(
                                conf_id,
                                approved=is_approve,
                                source=msg.channel,
                            )
                        reply_text = (
                            "✅ Approved — executing..."
                            if is_approve
                            else "❌ Cancelled."
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=reply_text,
                                metadata=self._with_trace_metadata(
                                    {"reply_to": msg.sender_id},
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                ),
                            )
                        )
                        return

            attachment_summary = self._build_attachment_summary(attachments)
            document_attachment_context = self._build_document_attachment_context(
                attachments
            )
            user_text_content = self._join_message_sections(
                content, attachment_summary, document_attachment_context
            )
            content = self._join_message_sections(content, attachment_summary) or content

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata=self._with_trace_metadata(
                        {"type": "typing"},
                        turn_id=turn_id,
                        message_id=assistant_message_id,
                    ),
                )
            )

            current_task = asyncio.current_task()
            if current_task:
                self.active_tasks[session_key] = current_task

            self.session_locks.setdefault(session_key, asyncio.Lock())

            async with self.session_locks[session_key]:
                try:
                    # ── Parallel: Auto-RAG + history load ───────────────────

                    rag_timeout_s = getattr(
                        getattr(self.config, "ai_harness", None),
                        "rag_timeout_s",
                        0.2,
                    )
                    rag_stage_started = time.perf_counter()
                    history_stage_started = time.perf_counter()

                    # Fast-path: skip RAG for short/casual messages.
                    async def _do_rag() -> Dict[str, Any]:
                        trace: Dict[str, Any] = {
                            "ts": time.time(),
                            "query": content,
                            "status": "skipped",
                            "mode": "none",
                            "results": [],
                            "recalled_context": "",
                        }
                        if not (
                            content
                            and len(content) > 10
                            and not content.startswith(("/", "@"))
                        ):
                            return trace
                        # Skip RAG for single casual words
                        if content.strip().lower() in _CASUAL_WORDS:
                            return trace
                        try:
                            results: list = []
                            if self.vector_service.has_semantic_candidate():
                                try:
                                    semantic_results = (
                                        await self.vector_service.search_semantic(
                                            content, limit=3
                                        )
                                        or []
                                    )
                                    results = [
                                        r
                                        for r in semantic_results
                                        if r.get("score", 1.0) >= AUTORAG_MIN_SCORE
                                    ]
                                    if results:
                                        trace["mode"] = "vector"
                                except Exception as e:
                                    logger.warning(f"Semantic search failed: {e}")
                            if not results:
                                results = (
                                    await self.vector_service.search_grep(
                                        content, limit=3
                                    )
                                    or []
                                )
                                if results:
                                    trace["mode"] = "grep_fallback"
                            if results:
                                seen, lines = set(), []
                                for r in results:
                                    text = r["text"].strip()
                                    if text not in seen:
                                        seen.add(text)
                                        lines.append(
                                            f"- {text} (Date: {r.get('timestamp', 'unknown')})"
                                        )
                                if lines:
                                    trace["status"] = "recalled"
                                    trace["results"] = [
                                        self._build_rag_result_trace(r) for r in results
                                    ]
                                    trace["recalled_context"] = "\n".join(lines)
                                    logger.info(
                                        f"Auto-RAG: {len(lines)} memories recalled."
                                    )
                                    return trace
                            trace["status"] = "miss"
                        except Exception as e:
                            trace["status"] = "error"
                            trace["error"] = str(e)
                            logger.warning(f"Auto-RAG failed: {e}")
                        return trace

                    async def _do_history_load():
                        if session_key not in self.history:
                            return await self.session_manager.load_history(session_key)
                        return None

                    _rag_needed = (
                        bool(content)
                        and len(content) > 10
                        and not content.startswith(("/", "@"))
                        and content.strip().lower() not in _CASUAL_WORDS
                    )

                    if _rag_needed:
                        rag_coro = asyncio.wait_for(_do_rag(), timeout=rag_timeout_s)
                        rag_task = asyncio.create_task(rag_coro)
                    else:
                        rag_task = None

                    hist_task = asyncio.create_task(_do_history_load())

                    # Start retrieval and disk history work before publishing the
                    # ephemeral activity update. This overlaps channel delivery
                    # latency with pre-inference preparation without changing the
                    # prompt or recall result.
                    await self._publish_activity(
                        msg,
                        "Recalling memory and loading history...",
                        turn_id=turn_id,
                        message_id=assistant_message_id,
                    )

                    rag_stage_metadata = {
                        "status": "skipped",
                        "mode": "none",
                        "timeout_s": rag_timeout_s,
                    }
                    if rag_task is not None:
                        try:
                            rag_result = await rag_task
                            recalled_context = rag_result.get("recalled_context", "")
                            self._record_rag_trace(session_key, rag_result)
                            rag_stage_metadata = {
                                "status": rag_result.get("status", "completed"),
                                "mode": rag_result.get("mode", "unknown"),
                                "timeout_s": rag_timeout_s,
                            }
                        except asyncio.TimeoutError:
                            recalled_context = ""
                            self._record_rag_trace(
                                session_key,
                                {
                                    "ts": time.time(),
                                    "query": content,
                                    "status": "timeout",
                                    "mode": "timeout",
                                    "results": [],
                                    "recalled_context": "",
                                },
                            )
                            rag_stage_metadata = {
                                "status": "timeout",
                                "mode": "timeout",
                                "timeout_s": rag_timeout_s,
                            }
                            logger.debug("Auto-RAG timeout - skipping.")
                        except Exception as e:
                            recalled_context = ""
                            self._record_rag_trace(
                                session_key,
                                {
                                    "ts": time.time(),
                                    "query": content,
                                    "status": "error",
                                    "mode": "error",
                                    "results": [],
                                    "recalled_context": "",
                                    "error": str(e),
                                },
                            )
                            rag_stage_metadata = {
                                "status": "error",
                                "mode": "error",
                                "timeout_s": rag_timeout_s,
                            }
                            logger.warning(f"Auto-RAG error: {e}")
                    else:
                        recalled_context = ""
                        self._record_rag_trace(
                            session_key,
                            {
                                "ts": time.time(),
                                "query": content,
                                "status": "skipped",
                                "mode": "none",
                                "results": [],
                                "recalled_context": "",
                            },
                        )
                        logger.debug("Auto-RAG skipped (short/casual message).")

                    self._record_stage_timing(
                        session_key,
                        "rag",
                        rag_stage_started,
                        metadata=rag_stage_metadata,
                    )
                    persisted = await hist_task
                    self._record_stage_timing(
                        session_key,
                        "history_load",
                        history_stage_started,
                        metadata={"restored": bool(persisted)},
                    )

                    sender_name = msg.metadata.get("sender_name", "")
                    ponytail_mode = normalize_ponytail_mode(
                        msg.metadata.get("ponytail_mode")
                    )
                    prompt_stage_started = time.perf_counter()
                    system_prompt = await self._build_full_system_prompt(
                        sender_id,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        recalled_context=recalled_context,
                        sender_name=sender_name,
                        current_message=content,
                        forced_skill_name=forced_skill_name,
                        ponytail_mode=ponytail_mode,
                        session_key=session_key,
                    )
                    prompt_stage_metadata = {
                        "has_recalled_context": bool(recalled_context)
                    }
                    if forced_skill_name:
                        prompt_stage_metadata["forced_skill"] = forced_skill_name
                    if ponytail_mode != "off":
                        prompt_stage_metadata["ponytail_mode"] = ponytail_mode
                    self._record_stage_timing(
                        session_key,
                        "prompt_build",
                        prompt_stage_started,
                        metadata=prompt_stage_metadata,
                    )

                    if session_key not in self.history:
                        if persisted:
                            self.history[session_key] = persisted
                            logger.info(
                                f"♻ Restored {session_key} ({len(persisted)} msgs)"
                            )
                        else:
                            self.history[session_key] = [
                                {"role": "system", "content": system_prompt}
                            ]

                    self.history[session_key][0] = {
                        "role": "system",
                        "content": system_prompt,
                    }

                    image_urls = self._collect_image_inputs(
                        attachments,
                        msg.metadata.get("images") or [],
                        str(msg.metadata.get("image") or "").strip(),
                    )

                    user_text_payload = user_text_content or content or " "
                    if image_urls and self._image_inputs_disabled_for_session(
                        session_key
                    ):
                        image_parts = [
                            {"type": "image_url", "image_url": {"url": image_url}}
                            for image_url in image_urls
                        ]
                        user_msg_content = self._render_text_only_message_content(
                            [{"type": "text", "text": user_text_payload}]
                            + image_parts
                        )
                    else:
                        user_msg_content = (
                            [{"type": "text", "text": user_text_payload}]
                            + [
                                {"type": "image_url", "image_url": {"url": image_url}}
                                for image_url in image_urls
                            ]
                            if image_urls
                            else user_text_payload
                        )
                    self.history[session_key].append(
                        {"role": "user", "content": user_msg_content}
                    )
                    self._mark_dirty(session_key)

                    asyncio.create_task(
                        self.session_manager.append_chat_log(
                            session_key,
                            {
                                "role": "user",
                                "content": msg.content or "",
                                "message_id": str(msg.metadata.get("message_id") or "").strip() or None,
                                "client_message_id": str(msg.metadata.get("client_message_id") or "").strip() or None,
                                "edited_from_message_id": str(msg.metadata.get("edited_from_message_id") or "").strip() or None,
                                "image": bool(image_urls),
                                "images": len(image_urls),
                                "attachments": [
                                    str(attachment.get("name") or "attachment")
                                    for attachment in attachments
                                ],
                            },
                        )
                    )

                    injected = [
                        "SOUL.md",
                        "IDENTITY.md",
                        f"memory/{datetime.now().strftime('%Y-%m-%d')}.md",
                    ]
                    if (USERS_DIR / f"{sender_id}.md").exists():
                        injected.append(f"users/{sender_id}.md")
                    for attachment in attachments:
                        path = str(attachment.get("path") or "").strip()
                        if path:
                            injected.append(path)

                    asyncio.create_task(
                        self.session_manager.update_session(
                            session_key=session_key,
                            model=self.model,
                            origin=msg.channel,
                            injected_files=injected,
                        )
                    )

                    selected_subagent = self.subagent_registry.get_default_selection()
                    should_route_via_selected_subagent = (
                        selected_subagent != "auto"
                        and not msg.metadata.get("is_report")
                        and not msg.metadata.get("is_confirmation")
                        and not msg.metadata.get("is_scheduler")
                    )
                    if should_route_via_selected_subagent:
                        routed_task = user_text_content or content or " "
                        self._log_session_event(
                            session_key,
                            {
                                "type": "subagent_mode_route",
                                "turn_id": turn_id,
                                "subagent": selected_subagent,
                                "task_preview": routed_task[:500],
                            },
                        )
                        await self._publish_activity(
                            msg,
                            f"Routing through {selected_subagent} mode...",
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        )
                        await self._trim_history(session_key)
                        asyncio.create_task(self._flush_history(session_key))
                        raw_reply = await self.run_subagent(
                            session_key,
                            f"{session_key}_sub_{uuid.uuid4().hex[:6]}",
                            routed_task,
                            agent_name=selected_subagent,
                        )
                        tag_result = await process_tags(
                            raw_reply=raw_reply,
                            sender_id=sender_id,
                            validate_soul=prompt_module.validate_and_save_soul,
                            validate_identity=prompt_module.validate_and_save_identity,
                            validate_mood=prompt_module.validate_and_save_mood,
                            validate_relationship=prompt_module.validate_and_save_relationships,
                            vector_service=self.vector_service,
                            bus=self.bus,
                            msg=msg,
                            config=self.config,
                        )
                        reply_to_user = tag_result.clean_reply or raw_reply
                        self.history[session_key].append(
                            {"role": "assistant", "content": raw_reply}
                        )
                        self._mark_dirty(session_key)
                        asyncio.create_task(
                            self.session_manager.append_chat_log(
                                session_key, {"role": "assistant", "content": raw_reply}
                            )
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=self._with_trace_metadata(
                                    {"type": "stop_typing"},
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                ),
                            )
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=reply_to_user,
                                metadata=self._with_trace_metadata(
                                    {"reply_to": msg.sender_id},
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                ),
                            )
                        )
                        return

                    await self._trim_history(session_key)
                    asyncio.create_task(self._flush_history(session_key))

                    plan_mode = self._is_read_only_plan_request(content, msg)
                    include_tools = bool(
                        (
                            forced_skill_name
                            or self._should_include_tools_for_turn(
                                content, session_key=session_key
                            )
                        )
                        and not plan_mode
                    )
                    self._log_tool_debug(
                        "tool_gate_decision",
                        session_key=session_key,
                        include_tools=include_tools,
                        content=content,
                    )
                    initial_tool_definitions: Optional[List[Dict[str, Any]]] = None
                    if include_tools:
                        tool_schema_started = time.perf_counter()
                        initial_tool_definitions = self._get_tool_definitions_for_turn(
                            content,
                            forced_skill_name=forced_skill_name,
                            session_key=session_key,
                        )
                        self._record_stage_timing(
                            session_key,
                            "tool_schema",
                            tool_schema_started,
                            metadata={
                                "tool_count": len(initial_tool_definitions),
                                "mode": getattr(
                                    getattr(self.config, "ai_harness", None),
                                    "mode",
                                    "balanced",
                                ),
                            },
                        )
                    initial_tool_choice = (
                        "required"
                        if include_tools
                        and self._requires_initial_tool_call(
                            content, initial_tool_definitions
                        )
                        else "auto"
                    )
                    llm_first_call_started = time.perf_counter()
                    plan_instruction = (
                        "This is read-only coding Plan mode. Do not call or propose tool "
                        "execution. Return a concise plan with intended files, rationale, "
                        "risks, acceptance checks, and exact verification commands. State "
                        "what still needs inspection instead of claiming an edit was made."
                    )
                    initial_messages = self.history[session_key]
                    if plan_mode:
                        initial_messages = initial_messages + [
                            {"role": "system", "content": plan_instruction}
                        ]
                    stream = await self._llm_call_with_retry(
                        messages=initial_messages,
                        session_key=session_key,
                        msg=msg,
                        stream=True,
                        include_tools=include_tools,
                        tool_context_text=content,
                        turn_id=turn_id,
                        message_id=assistant_message_id,
                        tool_definitions_override=initial_tool_definitions,
                        tool_choice=initial_tool_choice,
                    )
                    self._record_stage_timing(
                        session_key,
                        "llm_first_call",
                        llm_first_call_started,
                        metadata={
                            "include_tools": include_tools,
                            "tool_count": len(initial_tool_definitions or []),
                            "plan_mode": plan_mode,
                        },
                    )
                    stream_consume_started = time.perf_counter()
                    active_output_queued = make_output_queued_recorder("initial", 0)
                    consume_result = await self._consume_stream(
                        stream,
                        msg,
                        session_key,
                        turn_id=turn_id,
                        message_id=assistant_message_id,
                        turn_started_at=turn_started,
                        llm_started_at=llm_first_call_started,
                        iteration_kind="initial",
                        iteration=0,
                        on_output_queued=active_output_queued,
                    )
                    (
                        full_content,
                        tool_calls,
                        _,
                        streamed_to_web,
                        streamed_to_discord,
                    ) = (
                        self._unpack_stream_result(consume_result)
                    )
                    self._record_stage_timing(
                        session_key,
                        "stream_consume",
                        stream_consume_started,
                        metadata={"tool_call_count": len(tool_calls or [])},
                    )
                    accumulated_content = full_content
                    iterations_limit_reached = False
                    web_streamed_reply = bool(streamed_to_web)
                    discord_streamed_reply = bool(streamed_to_discord)
                    force_direct_reply = False
                    any_tool_calls_in_turn = bool(tool_calls)
                    fallback_inserted = False

                    if full_content or tool_calls:
                        am: Dict = {"role": "assistant", "content": full_content or ""}
                        if tool_calls:
                            am["tool_calls"] = tool_calls
                        self.history[session_key].append(am)
                        self._mark_dirty(session_key)

                    raw_reply = accumulated_content or ""
                    coding_turn = self._is_coding_turn(content, tool_calls)
                    repair_attempts = 0
                    repair_fingerprints: Set[str] = set()
                    recovery_blocked: Optional[str] = None
                    recovery_required = False
                    unresolved_tool_failure = False
                    unresolved_failure_detail = ""

                    if tool_calls:
                        if coding_turn:
                            await self._emit_coding_phase(
                                "inspect", session_key, msg, turn_id=turn_id,
                                message_id=assistant_message_id,
                            )
                            active_output_queued("tool_progress")
                            await self._emit_coding_phase(
                                "plan", session_key, msg, turn_id=turn_id,
                                message_id=assistant_message_id,
                            )
                            await self._stage_workspace_changeset(
                                tool_calls,
                                session_key,
                                msg,
                                turn_id=turn_id,
                                message_id=assistant_message_id,
                            )
                        await self._publish_activity(
                            msg,
                            "Planning tool calls...",
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        )
                        active_output_queued("tool_progress")
                        await self._publish_tool_intents(
                            tool_calls,
                            session_key,
                            msg,
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        )
                        if msg.channel == "web":
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content="",
                                    metadata=self._with_trace_metadata(
                                        {"type": "stop_typing"},
                                        turn_id=turn_id,
                                        message_id=assistant_message_id,
                                    ),
                                )
                            )

                        await self._publish_activity(
                            msg,
                            "Executing tools...",
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        )
                        tool_batch_started = time.perf_counter()
                        is_blocked = await self._execute_tool_batch(
                            tool_calls,
                            session_key,
                            msg,
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                            coding_turn=coding_turn,
                            on_progress_queued=active_output_queued,
                        )
                        tool_batch_duration_s += time.perf_counter() - tool_batch_started
                        tool_batch_count += 1
                        tool_batch_blocked = bool(is_blocked)

                        batch_failures = [
                            outcome
                            for outcome in getattr(self, "_last_tool_outcomes", [])
                            if not outcome.success
                        ]
                        unresolved_tool_failure = bool(batch_failures)
                        if batch_failures:
                            unresolved_failure_detail = batch_failures[-1].diagnostic_tail
                            repair_attempts, recovery_blocked = await self._queue_tool_recovery(
                                session_key,
                                msg,
                                repair_fingerprints,
                                repair_attempts,
                                coding_turn=coding_turn,
                                turn_id=turn_id,
                                message_id=assistant_message_id,
                            )
                            recovery_required = recovery_blocked is None

                        if recovery_blocked:
                            raw_reply = recovery_blocked
                            force_direct_reply = True
                        if not recovery_blocked:
                            max_iterations = getattr(self.config, "max_iterations", 30)
                            iteration = 0
                            artifact_requested = self._artifact_delivery_requested(content)
                            artifact_reserve_steps = max(
                                4, min(8, max(1, int(max_iterations)) // 4)
                            )
                            research_actions_used = sum(
                                1
                                for call in tool_calls
                                if str(call.get("function", {}).get("name") or "")
                                in _RESEARCH_TOOL_NAMES
                            )
                            research_action_limit = max(
                                8, min(18, max(1, int(max_iterations)) - 8)
                            )

                            while iteration < max_iterations:
                                iteration += 1

                                if iteration >= max_iterations:
                                    iterations_limit_reached = True
                                    logger.warning(
                                        f"⚠ Max iterations ({max_iterations}) reached."
                                    )
                                    self.metrics.record_anomaly(
                                        session_key,
                                        "tool_iteration_limit_reached",
                                        detail=f"limit={max_iterations}",
                                    )
                                    synthesis_instruction = (
                                        "Tool step cap reached. Produce a concise tools-disabled summary "
                                        "of completed actions, changed paths if known, the last diagnostic, "
                                        "remaining verification, and the safest next action. Never claim success "
                                        "without a passing verification."
                                    )
                                    try:
                                        synthesis_llm_started = time.perf_counter()
                                        active_output_queued = make_output_queued_recorder(
                                            "synthesis", iteration
                                        )
                                        normalized_history = self._normalize_history_in_place(
                                            session_key
                                        )
                                        synthesis_stream = await self._llm_call_with_retry(
                                            messages=normalized_history
                                            + [{"role": "system", "content": synthesis_instruction}],
                                            session_key=session_key,
                                            msg=msg,
                                            stream=True,
                                            include_tools=False,
                                            tool_context_text=content,
                                            turn_id=turn_id,
                                            message_id=assistant_message_id,
                                        )
                                        synthesis_result = await self._consume_stream(
                                            synthesis_stream,
                                            msg,
                                            session_key,
                                            turn_id=turn_id,
                                            message_id=assistant_message_id,
                                            turn_started_at=turn_started,
                                            llm_started_at=synthesis_llm_started,
                                            iteration_kind="synthesis",
                                            iteration=iteration,
                                            on_output_queued=active_output_queued,
                                        )
                                        synthesis_content, _, _, _, _ = self._unpack_stream_result(
                                            synthesis_result
                                        )
                                        raw_reply = str(synthesis_content or "").strip()
                                    except Exception as synthesis_error:
                                        logger.warning(
                                            f"Tool-cap synthesis failed: {synthesis_error}"
                                        )
                                        raw_reply = ""
                                    if not raw_reply:
                                        raw_reply = self._build_step_cap_fallback(session_key)
                                    self.history[session_key].append(
                                        {
                                            "role": "assistant",
                                            "content": raw_reply,
                                        }
                                    )
                                    self._mark_dirty(session_key)
                                    break

                                force_tool_image_reply = (
                                    session_key in self._sessions_pending_tool_image_reply
                                )
                                post_tool_llm_started = time.perf_counter()
                                active_output_queued = make_output_queued_recorder(
                                    "post_tool", iteration
                                )
                                normalized_history = self._normalize_history_in_place(
                                    session_key
                                )
                                post_tool_messages = normalized_history
                                post_tool_definitions = initial_tool_definitions
                                if (
                                    artifact_requested
                                    and (
                                        iteration
                                        >= max(1, max_iterations - artifact_reserve_steps)
                                        or research_actions_used >= research_action_limit
                                    )
                                ):
                                    post_tool_messages = normalized_history + [
                                        {
                                            "role": "system",
                                            "content": (
                                                "Artifact completion reserve is active. Stop all open-ended "
                                                "searching and browsing now. Use the evidence already gathered "
                                                "to create, verify, and deliver the requested file. If some facts "
                                                "remain uncertain, put explicit caveats and source URLs in the "
                                                "artifact instead of abandoning delivery. Use the literal value "
                                                "'Unverified' for every unsupported field and do not contradict "
                                                "that status with a numeric claim elsewhere in the same row."
                                            ),
                                        }
                                    ]
                                    post_tool_definitions = (
                                        self._artifact_reserve_tool_definitions(
                                            initial_tool_definitions
                                        )
                                    )
                                nxt_stream = await self._llm_call_with_retry(
                                    messages=post_tool_messages,
                                    session_key=session_key,
                                    msg=msg,
                                    stream=True,
                                    include_tools=not force_tool_image_reply,
                                    tool_context_text=content,
                                    tool_definitions_override=post_tool_definitions,
                                    tool_choice=(
                                        "required"
                                        if recovery_required and not force_tool_image_reply
                                        else "auto"
                                    ),
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                )
                                if force_tool_image_reply:
                                    self._sessions_pending_tool_image_reply.discard(
                                        session_key
                                    )
                                nxt_consume_result = await self._consume_stream(
                                    nxt_stream,
                                    msg,
                                    session_key,
                                    accumulated_content,
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                    turn_started_at=turn_started,
                                    llm_started_at=post_tool_llm_started,
                                    iteration_kind="post_tool",
                                    iteration=iteration,
                                    on_output_queued=active_output_queued,
                                )
                                (
                                    nxt_content,
                                    nxt_tool_calls,
                                    _,
                                    nxt_streamed_to_web,
                                    nxt_streamed_to_discord,
                                ) = (
                                    self._unpack_stream_result(nxt_consume_result)
                                )
                                web_streamed_reply = web_streamed_reply or bool(
                                    nxt_streamed_to_web
                                )
                                discord_streamed_reply = (
                                    discord_streamed_reply
                                    or bool(nxt_streamed_to_discord)
                                )

                                clean_next = self._dedup_overlap(
                                    accumulated_content, nxt_content
                                )

                                if clean_next:
                                    sep = ""
                                    if (
                                        accumulated_content
                                        and not accumulated_content.endswith(
                                            ("\n", " ")
                                        )
                                    ):
                                        if not clean_next.startswith(("\n", " ")):
                                            if (
                                                clean_next
                                                and clean_next[0] not in ".,!?;:"
                                            ):
                                                sep = " "
                                    accumulated_content += sep + clean_next

                                if not nxt_tool_calls:
                                    raw_reply = accumulated_content
                                    self.history[session_key].append(
                                        {
                                            "role": "assistant",
                                            "content": nxt_content,
                                        }
                                    )
                                    self._mark_dirty(session_key)
                                    break

                                nxt_am: Dict = {
                                    "role": "assistant",
                                    "content": nxt_content or "",
                                }
                                nxt_am["tool_calls"] = nxt_tool_calls
                                self.history[session_key].append(nxt_am)
                                self._mark_dirty(session_key)

                                research_actions_used += sum(
                                    1
                                    for call in nxt_tool_calls
                                    if str(call.get("function", {}).get("name") or "")
                                    in _RESEARCH_TOOL_NAMES
                                )

                                tool_batch_started = time.perf_counter()
                                nxt_blocked = await self._execute_tool_batch(
                                    nxt_tool_calls,
                                    session_key,
                                    msg,
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                    coding_turn=coding_turn,
                                    on_progress_queued=active_output_queued,
                                )
                                tool_batch_duration_s += (
                                    time.perf_counter() - tool_batch_started
                                )
                                tool_batch_count += 1
                                tool_batch_blocked = bool(nxt_blocked)

                                batch_failures = [
                                    outcome
                                    for outcome in getattr(self, "_last_tool_outcomes", [])
                                    if not outcome.success
                                ]
                                unresolved_tool_failure = bool(batch_failures)
                                if batch_failures:
                                    unresolved_failure_detail = batch_failures[-1].diagnostic_tail
                                    repair_attempts, recovery_blocked = await self._queue_tool_recovery(
                                        session_key,
                                        msg,
                                        repair_fingerprints,
                                        repair_attempts,
                                        coding_turn=coding_turn,
                                        turn_id=turn_id,
                                        message_id=assistant_message_id,
                                    )
                                    recovery_required = recovery_blocked is None
                                else:
                                    recovery_required = False
                                    unresolved_failure_detail = ""
                                if recovery_blocked:
                                    raw_reply = recovery_blocked
                                    force_direct_reply = True
                                    break

                                if iteration % _INTERIM_SAVE_EVERY == 0:
                                    await self._flush_history(session_key)

                                if nxt_blocked:
                                    logger.info(
                                        "Tool policy blocked a step; recovery remains active."
                                    )
                            if tool_batch_count:
                                try:
                                    self.metrics.record_stage_timing(
                                        session_key,
                                        "tool_batch",
                                        tool_batch_duration_s,
                                        metadata={
                                            "batch_count": tool_batch_count,
                                            "blocked": tool_batch_blocked,
                                        },
                                    )
                                except Exception:
                                    pass
                        else:
                            logger.info(
                                "Tool recovery stopped after the configured budget."
                            )
                    else:
                        raw_reply = full_content

                    if plan_mode and raw_reply:
                        await self._persist_coding_plan(
                            raw_reply,
                            msg,
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        )

                    if tool_calls and not str(raw_reply or "").strip():
                        self._log_tool_debug(
                            "tool_fallback_inserted",
                            session_key=session_key,
                            stage="post_initial_tool_batch",
                        )
                        raw_reply = self._build_tool_fallback_reply(session_key)
                        force_direct_reply = True
                        fallback_inserted = True
                        self.history[session_key].append(
                            {"role": "assistant", "content": raw_reply}
                        )
                        self._mark_dirty(session_key)

                    if coding_turn and raw_reply and not recovery_blocked:
                        await self._emit_coding_phase(
                            "complete",
                            session_key,
                            msg,
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        )

                    tag_result = await process_tags(
                        raw_reply=raw_reply,
                        sender_id=sender_id,
                        validate_soul=prompt_module.validate_and_save_soul,
                        validate_identity=prompt_module.validate_and_save_identity,
                        validate_mood=prompt_module.validate_and_save_mood,
                        validate_relationship=prompt_module.validate_and_save_relationships,
                        vector_service=self.vector_service,
                        bus=self.bus,
                        msg=msg,
                        config=self.config,
                    )
                    reply_to_user = tag_result.clean_reply
                    soul_updated = tag_result.soul_updated
                    identity_updated = tag_result.identity_updated

                    if any_tool_calls_in_turn and not str(reply_to_user or "").strip():
                        self._log_tool_debug(
                            "tool_fallback_inserted",
                            session_key=session_key,
                            stage="post_tag_processing",
                        )
                        fallback_reply = self._build_tool_fallback_reply(session_key)
                        reply_to_user = fallback_reply
                        raw_reply = fallback_reply
                        force_direct_reply = True
                        if not fallback_inserted:
                            self.history[session_key].append(
                                {"role": "assistant", "content": fallback_reply}
                            )
                            self._mark_dirty(session_key)
                            fallback_inserted = True
                    elif msg and not str(reply_to_user or "").strip():
                        logger.warning(
                            "Empty user-visible reply after tag processing; sending fallback."
                        )
                        fallback_reply = self._build_empty_reply_fallback()
                        reply_to_user = fallback_reply
                        raw_reply = fallback_reply
                        force_direct_reply = True

                    # ── Self-repetition dedup ────────────────────────────
                    reply_to_user = self._dedupe_repeated_reply_sections(reply_to_user)

                    if soul_updated or identity_updated:
                        self._invalidate_stable_prompt(sender_id)
                        logger.info("🔄 Stable prompt cache invalidated.")

                    if not tool_calls and not (full_content or tool_calls):
                        self.history[session_key].append(
                            {"role": "assistant", "content": raw_reply}
                        )
                        self._mark_dirty(session_key)

                    asyncio.create_task(
                        self.session_manager.append_chat_log(
                            session_key, {"role": "assistant", "content": raw_reply}
                        )
                    )

                    if reply_to_user:
                        meta: Dict = {"reply_to": msg.sender_id}
                        if identity_updated:
                            meta["identity_updated"] = True
                        if iterations_limit_reached:
                            meta["is_warning"] = True
                        meta = self._with_trace_metadata(
                            meta,
                            turn_id=turn_id,
                            message_id=assistant_message_id,
                        )
                        if unresolved_tool_failure:
                            meta["task_status"] = "failed"
                            meta["is_error"] = True

                        suppress_web_final_reply = (
                            self._should_suppress_web_final_reply(
                                channel=msg.channel,
                                any_tool_calls_in_turn=any_tool_calls_in_turn,
                                iterations_limit_reached=iterations_limit_reached,
                                web_streamed_reply=web_streamed_reply,
                                force_direct_reply=force_direct_reply,
                                reply_to_user=reply_to_user,
                                raw_reply=raw_reply,
                            )
                        )
                        if suppress_web_final_reply:
                            outbound = OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=self._with_trace_metadata(
                                    {"type": "stop_typing"},
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                ),
                            )
                        else:
                            from core.tts import ElevenLabsTTS

                            voice_cfg = ElevenLabsTTS.get_voice_config()
                            voice_on = bool(
                                voice_cfg.get("enabled", False)
                                and ElevenLabsTTS.get_api_key()
                            )
                            delivery_mode = self._resolve_voice_delivery(
                                msg.channel, voice_cfg, voice_on, bool(reply_to_user)
                            )
                            outbound = None

                            if delivery_mode in ("audio_only", "audio_and_text"):
                                # Chat channels: send the audio file itself (voice note).
                                audio_path = await self._synthesize_voice_file(
                                    reply_to_user
                                )
                                if audio_path:
                                    file_meta = self._with_trace_metadata(
                                        {
                                            "type": "file",
                                            "file_path": audio_path,
                                            "caption": "",
                                            "cleanup_file": True,
                                            "fallback_text": reply_to_user,
                                        },
                                        turn_id=turn_id,
                                        message_id=assistant_message_id,
                                    )
                                    await self.bus.publish_outbound(
                                        OutboundMessage(
                                            channel=msg.channel,
                                            chat_id=msg.chat_id,
                                            content="",
                                            metadata=file_meta,
                                        )
                                    )
                                    active_output_queued("audio")
                                    # Optionally also send the text as a separate message.
                                    if delivery_mode == "audio_and_text":
                                        outbound = OutboundMessage(
                                            channel=msg.channel,
                                            chat_id=msg.chat_id,
                                            content=reply_to_user,
                                            metadata=meta,
                                        )
                                    # else: audio only — nothing more to send.
                                else:
                                    # Synthesis failed → fall back to text so the user still gets a reply.
                                    outbound = OutboundMessage(
                                        channel=msg.channel,
                                        chat_id=msg.chat_id,
                                        content=reply_to_user,
                                        metadata=meta,
                                    )
                            else:
                                # Web auto-TTS attaches a playable URL alongside the text.
                                if delivery_mode == "web_url":
                                    try:
                                        audio_url = await ElevenLabsTTS.synthesize_and_save(
                                            reply_to_user
                                        )
                                        if audio_url:
                                            meta["voice_url"] = audio_url
                                    except Exception as tts_err:
                                        logger.error(
                                            f"[TTS] Auto synthesis failed: {tts_err}"
                                        )
                                outbound = OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content=reply_to_user,
                                    metadata=meta,
                                )
                        if outbound is not None:
                            await self.bus.publish_outbound(outbound)
                            if outbound.content:
                                active_output_queued("content")
                            self._log_session_event(
                                session_key,
                                {
                                    "type": "outbound_message",
                                    "turn_id": turn_id,
                                    "channel": outbound.channel,
                                    "chat_id": outbound.chat_id,
                                    "content_preview": (outbound.content or "")[:500],
                                    "metadata_type": outbound.metadata.get("type"),
                                    "event_id": outbound.metadata.get("event_id"),
                                    "task_id": outbound.metadata.get("task_id"),
                                },
                            )

                except asyncio.CancelledError:
                    logger.warning(f"⚠ Task cancelled for {session_key}")
                    self._log_session_event(
                        session_key,
                        {"type": "turn_cancelled", "turn_id": turn_id},
                    )
                    import contextlib

                    with contextlib.suppress(Exception):
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel if msg else "web",
                                chat_id=msg.chat_id if msg else "system",
                                content="",
                                metadata=self._with_trace_metadata(
                                    {
                                        "type": "cancellation",
                                        "is_cancellation": True,
                                    },
                                    turn_id=turn_id,
                                    message_id=assistant_message_id,
                                ),
                            )
                        )
                    raise
                except Exception as e:
                    import traceback

                    traceback.print_exc()
                    logger.exception(f"❌ Error processing message: {e}")
                    await _tracker.complete_task(_msg_task_id, error=str(e)) if _msg_task_id else None
                    self._log_session_event(
                        session_key,
                        {
                            "type": "turn_error",
                            "turn_id": turn_id,
                            "error": str(e)[:500],
                        },
                    )
                    error_text = str(e)
                    lower_error = error_text.lower()
                    if (
                        "rate limit" in lower_error
                        or "rate_limit" in lower_error
                        or "usage_limit" in lower_error
                        or "usage limit" in lower_error
                        or "limit has been reached" in lower_error
                        or "limit_reached" in lower_error
                        or "codex provider returned an error" in lower_error
                        or "no visible response" in lower_error
                        or "429" in lower_error
                    ):
                        visible_error = (
                            "⚠ Rate limit or usage limit reached for the active AI model.\n"
                            f"`{error_text}`\n\n"
                            "Your ChatGPT Plus/Pro plan or API quota limit has been hit. Please wait for the limit to reset, "
                            "check your provider's account limits, or configure a fallback API model."
                        )
                    else:
                        visible_error = f"🚫 **Internal error.**\n`{e}`"
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=visible_error,
                            metadata=self._with_trace_metadata(
                                {"is_error": True, "reply_to": msg.sender_id},
                                turn_id=turn_id,
                                message_id=assistant_message_id,
                            ),
                        )
                    )

        finally:
            self._record_stage_timing(
                session_key,
                "turn_total",
                turn_started,
                metadata={"channel": getattr(msg, "channel", "unknown")},
            )
            if _msg_task_id:
                task = await _tracker.get_task(_msg_task_id)
                if task and task.status == "running":
                    current = asyncio.current_task()
                    is_cancelled = bool(
                        current
                        and getattr(current, "cancelling", lambda: 0)()
                    )
                    if is_cancelled:
                        await _tracker.update_task(
                            _msg_task_id,
                            status="cancelled",
                            error="Inbound turn cancelled.",
                        )
                    elif unresolved_tool_failure:
                        await _tracker.update_task(
                            _msg_task_id,
                            status="failed",
                            error=(
                                "Tool recovery exhausted or ended with an unresolved "
                                "failure: "
                                + redact_sensitive_text(unresolved_failure_detail)[:500]
                            ),
                        )
                    else:
                        await _tracker.complete_task(_msg_task_id)
            self._evict_history_image_inputs(session_key)
            await self._flush_history(session_key, force=True)
            current_task = asyncio.current_task()
            if self.active_tasks.get(session_key) is current_task:
                self.active_tasks.pop(session_key, None)
            # Always notify frontend that processing is done
            try:
                if msg:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=self._with_trace_metadata(
                                {"type": "stop_typing"},
                                turn_id=turn_id,
                                message_id=assistant_message_id,
                            ),
                        )
                    )
            except Exception:
                pass
            if task_context_token is not None:
                _CURRENT_TASK_ID.reset(task_context_token)
