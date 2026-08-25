"""Web channel implementation using FastAPI and WebSockets."""

import asyncio
import uuid
from dataclasses import asdict
import base64
import binascii
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote_to_bytes

import uvicorn
from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    Request,
    Header,
    Depends,
    HTTPException,
    status,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from channels.base import BaseChannel
from core.bus import EPHEMERAL_OUTBOUND_TYPES, MessageBus
from core.events import OutboundMessage
from core.llm_client import ChatRequest, LimeLLMClient
from core.prompt_modes import normalize_ponytail_mode
from core.oauth_profiles import get_codex_oauth_status
from core.runtime_paths import (
    get_allowed_paths_file,
    get_config_file,
    get_env_file,
    get_skill_dirs,
)
from core.session_manager import SessionManager
from core.tools import Toolbox

_CONTACTS_PATH_REL = ("data", "contacts.json")
_MAX_WEB_ATTACHMENT_BYTES = 8 * 1024 * 1024
_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".doc", ".docx"})
_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
_SECRET_CONFIG_KEYS = frozenset(
    {
        "APP_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "NVIDIA_API_KEY",
        "DASHSCOPE_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "SERPAPI_API_KEY",
        "ELEVENLABS_API_KEY",
        "DISCORD_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    }
)
_PIAI_MODELS_JS_PATH = (
    Path.cwd()
    / "node_modules"
    / "@earendil-works"
    / "pi-ai"
    / "dist"
    / "models.generated.js"
)
_PIAI_PROVIDER_MODEL_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_OPENAI_CURATED_MODELS = [
    {
        "id": "openai/gpt-5.6-sol",
        "name": "GPT-5.6 Sol",
        "provider": "openai",
    },
    {
        "id": "openai/gpt-5.6-terra",
        "name": "GPT-5.6 Terra",
        "provider": "openai",
    },
    {
        "id": "openai/gpt-5.6-luna",
        "name": "GPT-5.6 Luna",
        "provider": "openai",
    },
    {
        "id": "openai/gpt-5.5",
        "name": "GPT-5.5",
        "provider": "openai",
    },
    {
        "id": "openai/gpt-5.4",
        "name": "GPT-5.4",
        "provider": "openai",
    },
]
_CODEX_FALLBACK_MODELS = [
    {
        "id": "openai-codex/gpt-5.6-sol",
        "name": "GPT-5.6 Sol",
        "provider": "openai-codex",
    },
    {
        "id": "openai-codex/gpt-5.6-luna",
        "name": "GPT-5.6 Luna",
        "provider": "openai-codex",
    },
    {
        "id": "openai-codex/gpt-5.6-terra",
        "name": "GPT-5.6 Terra",
        "provider": "openai-codex",
    },
    {"id": "openai-codex/gpt-5.5", "name": "GPT-5.5", "provider": "openai-codex"},
    {"id": "openai-codex/gpt-5.4", "name": "GPT-5.4", "provider": "openai-codex"},
    {
        "id": "openai-codex/gpt-5.4-mini",
        "name": "GPT-5.4 Mini",
        "provider": "openai-codex",
    },
]
_SUPPORTED_CODEX_MODEL_IDS = frozenset(model["id"] for model in _CODEX_FALLBACK_MODELS)

_LOOPBACK_WEB_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def resolve_web_bind_host(
    requested_host: str,
    *,
    has_api_key: bool,
    trusted_proxy_only: bool = False,
) -> str:
    """Keep unauthenticated off-box binding limited to a private proxy network."""
    host = str(requested_host or "127.0.0.1").strip() or "127.0.0.1"
    if host in _LOOPBACK_WEB_HOSTS or has_api_key or trusted_proxy_only:
        return host
    return "127.0.0.1"

_SETUP_STATE_PATH = Path("data/setup-state.json")
_SETUP_LLM_PROBE_TIMEOUT_SECONDS = 20.0
_MOONSHOT_ENV_ALIASES = ("MOONSHOTAI_API_KEY", "KIMI_API_KEY")
_ALLOWED_SETUP_ENV_KEYS = {
    "LLM_MODEL",
    "APP_API_KEY",
    "ALLOWED_PATHS",
    "LLM_BASE_URL",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "NVIDIA_API_KEY",
    "DISCORD_TOKEN",
    "ENABLE_WHATSAPP",
    "WHATSAPP_BRIDGE_URL",
    "ENABLE_DYNAMIC_PERSONALITY",
    "VIDEO_WHISPER_ENABLED",
}


def _schedule_restart():
    asyncio.get_running_loop().call_later(1.0, _spawn_restart)


def _merge_env_lines(
    lines: list[str], updates: dict[str, str], clear_keys: set[str]
) -> list[str]:
    result = []
    processed_keys = set()
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key, val = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                result.append(f"{key}={updates[key]}")
                processed_keys.add(key)
            elif key in clear_keys:
                result.append(f"{key}=")
                processed_keys.add(key)
            else:
                result.append(line)
        else:
            result.append(line)
    for key, val in updates.items():
        if key not in processed_keys:
            result.append(f"{key}={val}")
    for key in clear_keys:
        if key not in processed_keys:
            result.append(f"{key}=")
    return result


def _redact_sensitive_data(val: Any) -> Any:
    if isinstance(val, dict):
        cleaned = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("api_key", "password", "token", "secret")) or k_lower == "key":
                continue
            elif k_lower in ("path", "command"):
                continue
            else:
                cleaned[k] = _redact_sensitive_data(v)
        return cleaned
    elif isinstance(val, list):
        return [_redact_sensitive_data(x) for x in val]
    elif isinstance(val, str):
        return val
    else:
        return val


def _serialize_workspace_for_app(workspace) -> dict:
    return {
        "workspace_id": workspace.workspace_id,
        "title": workspace.title,
        "origin": workspace.origin,
        "status": workspace.status,
        "session_key": workspace.session_key,
        "chat_id": workspace.chat_id,
        "parent_workspace_id": workspace.parent_workspace_id,
        "created_at": workspace.created_at,
        "updated_at": workspace.updated_at,
        "started_at": workspace.started_at,
        "completed_at": workspace.completed_at,
        "error": workspace.error,
        "attempts": [_serialize_attempt_for_app(a) for a in workspace.attempts],
        "artifacts": [_serialize_artifact_for_app(art) for art in workspace.artifacts],
        "metadata": _redact_sensitive_data(workspace.metadata),
    }


def _serialize_attempt_for_app(attempt) -> dict:
    return {
        "attempt_id": attempt.attempt_id,
        "status": attempt.status,
        "model": attempt.model,
        "summary": attempt.summary,
        "created_at": attempt.created_at,
        "updated_at": attempt.updated_at,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "error": attempt.error,
        "metadata": _redact_sensitive_data(attempt.metadata),
    }


def _serialize_task_for_app(task) -> dict:
    """Expose task identity/state without live handles or sensitive inputs."""
    return {
        "task_id": task.task_id,
        "type": task.type,
        "status": task.status,
        "channel": task.channel,
        "session_key": task.session_key,
        "chat_id": task.chat_id,
        "summary": str(task.summary or "")[:500],
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "parent_task_id": task.parent_task_id,
        "attempt": task.attempt,
        "error": str(task.error or "")[:500],
        "metadata": _redact_sensitive_data(task.metadata),
    }


def _serialize_artifact_for_app(artifact) -> dict:
    serialized = {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "title": artifact.title,
        "created_at": artifact.created_at,
        "available_locally": bool(artifact.path),
    }
    if artifact.kind == "change_set":
        from core.review_entrypoint import changeset_for_app

        serialized["changeset"] = changeset_for_app(artifact.metadata)
    elif artifact.kind == "coding_plan":
        serialized["plan"] = {
            "status": str(artifact.metadata.get("status") or "planned"),
            "summary": _redact_sensitive_data(
                str(artifact.metadata.get("summary") or "")[:2000]
            ),
        }
    else:
        serialized["metadata"] = _redact_sensitive_data(artifact.metadata)
    return serialized


def _serialize_pending_approval_for_app(conf_id, conf) -> dict:
    preview = conf.get("preview") or {}
    clean_preview = {}
    for k, v in preview.items():
        if k != "command":
            clean_preview[k] = _redact_sensitive_data(v)
    return {
        "conf_id": conf_id,
        "tool": conf.get("tool"),
        "session_key": conf.get("session_key"),
        "policy_profile": conf.get("policy_profile"),
        "decision_reason": conf.get("decision_reason"),
        "client_source": conf.get("client_source"),
        "preview": clean_preview,
    }


def _serialize_readiness_for_app(readiness) -> dict:
    if not isinstance(readiness, dict):
        return {}
    clean_readiness = {}
    for k, v in readiness.items():
        if "secret" not in k.lower():
            clean_readiness[k] = _redact_sensitive_data(v)
    return clean_readiness


def _sanitize_web_session_key(chat_id: str) -> str:
    session_key = f"web_{chat_id}"
    for char in ["/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
        session_key = session_key.replace(char, "_")
    return session_key


def _canonicalize_app_workspace_session_key(value: Any) -> str:
    """Return a bounded, filesystem-safe session key for an app workspace."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("web_"):
        raw = raw[4:]
    if not raw:
        return ""
    return _sanitize_web_session_key(raw)[:180]


def _app_attempt_terminal_outcome(metadata: dict, msg_type: str) -> tuple[str, str] | None:
    """Return a terminal app-attempt state for final outbound turn messages only."""
    task_status = str(metadata.get("task_status") or "").strip().lower()
    if task_status in {"completed", "failed", "cancelled"}:
        return (task_status, str(metadata.get("error") or "")[:200])
    if msg_type in {"cancellation", "turn_cancelled", "task_cancelled"} or metadata.get(
        "is_cancellation"
    ):
        return ("cancelled", "Cancelled by user.")
    is_final_reply = bool(metadata.get("reply_to")) or msg_type in {
        "terminal_error",
        "turn_error",
    }
    if metadata.get("is_error") and is_final_reply:
        error_code = str(metadata.get("error_code") or "agent_error").strip()
        return ("failed", error_code[:200])
    if msg_type == "message" and metadata.get("reply_to"):
        return ("completed", "")
    return None


def _extract_client_prompt_metadata(msg: Any) -> dict[str, str]:
    if not isinstance(msg, dict):
        return {}

    client_metadata = msg.get("metadata")
    if not isinstance(client_metadata, dict):
        return {}

    extracted: dict[str, str] = {}
    ponytail_mode = normalize_ponytail_mode(client_metadata.get("ponytail_mode"))
    if ponytail_mode != "off":
        extracted["ponytail_mode"] = ponytail_mode

    skill_name = str(client_metadata.get("skill_name") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", skill_name):
        extracted["skill_name"] = skill_name

    return extracted


def _mask_secret(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 4:
        return "•" * len(cleaned)
    return f"{'•' * max(4, min(len(cleaned) - 4, 12))}{cleaned[-4:]}"


def _serialize_secret(value: str) -> dict[str, Any]:
    cleaned = (value or "").strip()
    return {
        "configured": bool(cleaned),
        "masked": _mask_secret(cleaned),
        "last4": cleaned[-4:] if len(cleaned) > 4 else "",
    }


def _extract_js_object_block(text: str, key: str) -> str:
    marker = f'"{key}": {{'
    start = text.find(marker)
    if start < 0:
        return ""

    brace_start = text.find("{", start)
    if brace_start < 0:
        return ""

    depth = 0
    for idx in range(brace_start, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start + 1 : idx]
    return ""


def _load_piai_provider_models(provider: str) -> list[dict[str, str]]:
    # pi-ai 0.80+ split provider catalogs into dist/providers/*.models.js.
    # Keep the generated aggregate registry as a fallback for older releases
    # and for tests that inject a synthetic registry path.
    provider_registry_path = (
        _PIAI_MODELS_JS_PATH.parent / "providers" / f"{provider}.models.js"
    )
    registry_path = (
        provider_registry_path
        if provider_registry_path.exists()
        else _PIAI_MODELS_JS_PATH
    )
    try:
        stat = registry_path.stat()
    except OSError:
        return []

    cached = _PIAI_PROVIDER_MODEL_CACHE.get(provider)
    if cached and cached[0] == stat.st_mtime:
        return [dict(model) for model in cached[1]]

    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Failed to read pi-ai model registry: {exc}")
        return []

    block = (
        text
        if registry_path == provider_registry_path
        else _extract_js_object_block(text, provider)
    )
    if not block:
        return []

    pattern = re.compile(
        r'"(?P<id>[^"]+)":\s*{\s*id:\s*"(?P=id)",\s*name:\s*"(?P<name>[^"]+)"',
        re.DOTALL,
    )
    models = [
        {
            "id": f"{provider}/{match.group('id')}",
            "name": match.group("name"),
            "provider": provider,
        }
        for match in pattern.finditer(block)
    ]
    _PIAI_PROVIDER_MODEL_CACHE[provider] = (
        stat.st_mtime,
        [dict(model) for model in models],
    )
    return models


def _filter_supported_codex_models(models: list[dict[str, str]]) -> list[dict[str, str]]:
    filtered = [model for model in models if model.get("id") in _SUPPORTED_CODEX_MODEL_IDS]
    return filtered or [dict(model) for model in _CODEX_FALLBACK_MODELS]


def _build_identity_markdown(data: dict[str, Any]) -> str:
    lines = ["# IDENTITY.md - Who I Am", ""]
    lines.append(f"*   **Name:** {data.get('name', '')}")
    lines.append(f"*   **Emoji:** {data.get('emoji', '')}")
    lines.append(f"*   **Pfp_URL:** {data.get('pfp_url', '')}")
    lines.append(f"*   **Style:** {data.get('style', '')}")
    lines.append(f"*   **Catchphrases:** {data.get('catchphrases', '')}")
    lines.append(f"*   **Interests:** {data.get('interests', '')}")
    lines.append(f"*   **Birthday:** {data.get('birthday', '')}")
    lines.append(f"*   **Discord Style:** {data.get('discord_style', '')}")
    lines.append(f"*   **Telegram Style:** {data.get('telegram_style', '')}")
    lines.append(f"*   **WhatsApp Style:** {data.get('whatsapp_style', '')}")
    lines.append(f"*   **Web Style:** {data.get('web_style', '')}")
    lines.append(f"*   **Reaction Emojis:** {data.get('reaction_emojis', '')}")
    lines.append("")
    return "\n".join(lines)


def _resolve_channel_style(identity_data: dict[str, Any], channel: str) -> tuple[str, str]:
    platform_style = (identity_data.get(f"{channel}_style") or "").strip()
    web_style = (identity_data.get("web_style") or "").strip()
    general_style = (identity_data.get("style") or "").strip()
    if platform_style:
        return platform_style, f"{channel.title()} override"
    if web_style:
        return web_style, "Web fallback"
    return general_style, "Base style"


def _contacts_path():
    from pathlib import Path

    return Path.cwd().joinpath(*_CONTACTS_PATH_REL)


def _load_contacts() -> dict:
    p = _contacts_path()
    if not p.exists():
        return {"allowed": [], "pending": [], "blocked": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error loading contacts: {e}")
        return {"allowed": [], "pending": [], "blocked": []}


def _save_contacts(contacts: dict) -> None:
    p = _contacts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(contacts, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Error saving contacts: {e}")


class WebChannel(BaseChannel):
    """
    Web channel that serves a WebSocket endpoint via FastAPI.
    """

    name = "web"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        session_manager: Optional[SessionManager] = None,
    ):
        super().__init__(config, bus)
        self.app = FastAPI()
        self.server = None
        self.session_manager = session_manager or SessionManager()
        self.allowed_origins = self._get_allowed_origins()

        async def verify_api_key(request: Request, x_api_key: str = Header(None)):
            if not self._is_auth_required():
                return True
            internal_key = getattr(self.config.whitelist, "api_key", None)
            if x_api_key != internal_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing API Key",
                )
            return True

        self.verify_auth = verify_api_key
        self._setup_routes()

        self.active_connections: set[WebSocket] = set()
        self.app_connections: set[WebSocket] = set()
        self._app_chat_workspaces: dict[str, str] = {}
        self._app_chat_sessions: dict[str, str] = {}
        self._app_workspace_attempts: dict[str, list[str]] = {}
        self._app_event_sequences: dict[str, int] = {}
        self._boot_id = uuid.uuid4().hex

        self.channels = []
        self._whatsapp_qr: str | None = None
        self.start_time = time.time()
        self.actual_port = getattr(self.config.web, "port", 8000)
        self.scheduler = None

        self.llm_client = LimeLLMClient()
        self._provider_models_cache: dict[str, list] = {}
        self._provider_models_last_update: dict[str, float] = {}

    def set_scheduler(self, scheduler: Any):
        self.scheduler = scheduler

    def set_agent(self, agent: Any):
        self.agent = agent

    def set_channels(self, channels: list):
        self.channels = channels

    def _is_auth_required(self) -> bool:
        return bool(getattr(self.config.whitelist, "api_key", None))

    async def verify_app_auth(self, request: Request, x_api_key: str = Header(None)):
        internal_key = getattr(self.config.whitelist, "api_key", None)
        if not internal_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="APP_API_KEY is not configured",
            )
        if x_api_key != internal_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API Key",
            )
        return True

    def _get_allowed_origins(self) -> list[str]:
        origins = getattr(self.config.web, "allowed_origins", None) or []
        return [origin for origin in origins if origin != "*"]

    @staticmethod
    async def _read_text(path, encoding: str = "utf-8") -> str:
        return await asyncio.to_thread(path.read_text, encoding=encoding)

    @staticmethod
    async def _write_text(path, content: str, encoding: str = "utf-8") -> None:
        await asyncio.to_thread(path.write_text, content, encoding=encoding)

    @staticmethod
    async def _read_json(path, default: Any = None) -> Any:
        if not path.exists():
            return default
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return json.loads(raw)

    @staticmethod
    async def _write_json(path, data: Any) -> None:
        payload = await asyncio.to_thread(json.dumps, data, indent=2)
        await asyncio.to_thread(path.write_text, payload, encoding="utf-8")

    @staticmethod
    async def _tail_log_file(path, lines: int) -> list[str]:
        def _read_tail() -> list[str]:
            chunk_size = 8192
            result_lines: list[str] = []
            with path.open("rb") as f:
                f.seek(0, 2)
                remaining = f.tell()
                buffer = b""
                while remaining > 0 and len(result_lines) <= lines:
                    read_size = min(chunk_size, remaining)
                    remaining -= read_size
                    f.seek(remaining)
                    buffer = f.read(read_size) + buffer
                    result_lines = buffer.decode("utf-8", errors="replace").splitlines()
            return result_lines[-lines:]

        return await asyncio.to_thread(_read_tail)

    @staticmethod
    async def _load_contacts_async() -> dict:
        return await asyncio.to_thread(_load_contacts)

    @staticmethod
    async def _save_contacts_async(contacts: dict) -> None:
        await asyncio.to_thread(_save_contacts, contacts)

    @staticmethod
    def _sanitize_upload_component(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "upload")).strip("._")
        return safe or "upload"

    @staticmethod
    def _decode_data_url(data_url: str) -> tuple[str, bytes]:
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            raise ValueError("Attachment payload must be a valid data URL.")
        if "," not in data_url:
            raise ValueError("Attachment payload is malformed.")

        header, payload = data_url.split(",", 1)
        mime_type = header[5:].split(";", 1)[0].strip().lower()
        try:
            if ";base64" in header.lower():
                content = base64.b64decode(payload, validate=True)
            else:
                content = unquote_to_bytes(payload)
        except (binascii.Error, ValueError) as e:
            raise ValueError("Attachment payload could not be decoded.") from e

        return mime_type or "application/octet-stream", content

    @staticmethod
    def _extract_web_document_text(path) -> tuple[str, str | None]:
        suffix = path.suffix.lower()
        if suffix == ".doc":
            return (
                "",
                "Legacy .doc files are stored, but automatic text extraction is only available for .docx and .pdf.",
            )

        try:
            if suffix == ".docx":
                extracted = Toolbox._extract_docx_text(path)
            elif suffix == ".pdf":
                extracted = Toolbox._extract_pdf_text(path)
            else:
                return "", None
            return Toolbox._slice_text_for_read(extracted, max_chars=12_000), None
        except Exception as e:
            return "", str(e)

    async def _normalize_web_attachments(
        self, chat_id: str, raw_attachments: Any
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not isinstance(raw_attachments, list):
            return [], None

        from pathlib import Path

        safe_chat_id = self._sanitize_upload_component(chat_id)
        temp_dir = (Path.cwd() / "temp").resolve()
        upload_dir = temp_dir / "web_uploads" / safe_chat_id
        await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)

        attachments: list[dict[str, Any]] = []
        first_image_data_url: str | None = None

        for index, item in enumerate(raw_attachments[:4]):
            if not isinstance(item, dict):
                continue

            data_url = item.get("data_url") or item.get("url")
            if not data_url:
                continue

            mime_type, blob = self._decode_data_url(str(data_url))
            provided_mime = (
                str(item.get("mimeType") or item.get("mime_type") or item.get("type") or "")
                .strip()
                .lower()
            )
            if provided_mime:
                mime_type = provided_mime

            if len(blob) > _MAX_WEB_ATTACHMENT_BYTES:
                raise ValueError("Attachments are limited to 8 MB.")

            original_name = str(item.get("name") or f"attachment-{index + 1}").strip()
            original_name = Path(original_name).name or f"attachment-{index + 1}"
            suffix = Path(original_name).suffix.lower()
            is_image = mime_type.startswith("image/")
            is_document = mime_type in _DOCUMENT_MIME_TYPES or suffix in _DOCUMENT_EXTENSIONS

            if not is_image and not is_document:
                raise ValueError("Only images, PDF, DOC, and DOCX files are supported in web chat.")

            if not suffix:
                if mime_type == "application/pdf":
                    suffix = ".pdf"
                elif mime_type == "application/msword":
                    suffix = ".doc"
                elif (
                    mime_type
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ):
                    suffix = ".docx"
                elif is_image:
                    suffix = ".png"

            stem = self._sanitize_upload_component(Path(original_name).stem)
            stored_name = f"{int(time.time() * 1000)}_{index}_{stem}{suffix}"
            saved_path = upload_dir / stored_name
            await asyncio.to_thread(saved_path.write_bytes, blob)

            relative_path = saved_path.relative_to(Path.cwd()).as_posix()
            public_url = f"/temp/{saved_path.relative_to(temp_dir).as_posix()}"
            attachment: dict[str, Any] = {
                "name": original_name,
                "kind": "image" if is_image else "document",
                "mime_type": mime_type,
                "mimeType": mime_type,
                "path": relative_path,
                "url": public_url,
            }

            if is_image:
                first_image_data_url = first_image_data_url or str(data_url)
            else:
                extracted_text, extraction_note = self._extract_web_document_text(
                    saved_path
                )
                if extracted_text:
                    attachment["extracted_text"] = extracted_text
                if extraction_note:
                    attachment["extraction_note"] = extraction_note

            attachments.append(attachment)

        return attachments, first_image_data_url

    @staticmethod
    async def _close_websocket_safely(
        websocket: WebSocket, code: int = status.WS_1008_POLICY_VIOLATION
    ) -> None:
        try:
            await websocket.close(code=code)
        except RuntimeError:
            pass
        except Exception as e:
            logger.debug(f"WebSocket close skipped: {e}")

    async def _authenticate_websocket(self, websocket: WebSocket) -> bool:
        if not self._is_auth_required():
            return True
        internal_key = getattr(self.config.whitelist, "api_key", None)

        async def _send_auth_ok() -> bool:
            try:
                await websocket.send_text(json.dumps({"type": "auth_ok"}))
                return True
            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected during auth from {websocket.client}")
                return False
            except RuntimeError as e:
                logger.info(
                    f"WebSocket closed before auth completed from {websocket.client}: {e}"
                )
                return False

        header_key = websocket.headers.get("x-api-key")
        if header_key == internal_key:
            return await _send_auth_ok()

        query_key = websocket.query_params.get("api_key")
        if query_key == internal_key:
            return await _send_auth_ok()

        try:
            auth_frame = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            payload = json.loads(auth_frame)
        except asyncio.TimeoutError:
            logger.warning(f"WebSocket rejected: auth timeout from {websocket.client}")
            await self._close_websocket_safely(websocket)
            return False
        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected before auth from {websocket.client}")
            return False
        except json.JSONDecodeError:
            logger.warning(f"WebSocket rejected: invalid auth payload from {websocket.client}")
            await self._close_websocket_safely(websocket)
            return False
        except Exception:
            logger.warning(f"WebSocket rejected: invalid auth payload from {websocket.client}")
            await self._close_websocket_safely(websocket)
            return False

        if payload.get("type") != "auth" or payload.get("api_key") != internal_key:
            logger.warning(f"WebSocket rejected: bad API key from {websocket.client}")
            await self._close_websocket_safely(websocket)
            return False

        return await _send_auth_ok()

    def _setup_routes(self):
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=self.allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        from pathlib import Path

        temp_dir = Path("temp")
        temp_dir.mkdir(exist_ok=True)
        self.app.mount("/temp", StaticFiles(directory="temp"), name="temp")

        @self.app.get("/api/identity")
        async def get_identity():
            from core.prompt import get_identity_data

            return await asyncio.to_thread(get_identity_data)

        @self.app.get("/api/persona", dependencies=[Depends(self.verify_auth)])
        async def get_persona():
            from core.prompt import get_identity_data, SOUL_FILE, MOOD_FILE, USERS_DIR
            import re

            result = await asyncio.to_thread(get_identity_data)
            result["soul_summary"] = ""
            if SOUL_FILE.exists():
                try:
                    soul_content = await self._read_text(SOUL_FILE)
                    lines = soul_content.strip().split("\n")
                    if lines and lines[0].startswith("#"):
                        lines = lines[1:]
                    result["soul_summary"] = " ".join(lines)[:300].strip()
                except Exception as e:
                    logger.error(f"Error reading soul summary: {e}")

            result["mood"] = ""
            if MOOD_FILE.exists():
                result["mood"] = (await self._read_text(MOOD_FILE)).strip()

            from config import load_config

            cfg = await asyncio.to_thread(load_config)
            result["enable_dynamic_personality"] = getattr(
                cfg.llm, "enable_dynamic_personality", False
            )

            relationships = []
            if USERS_DIR.exists():
                for user_file in USERS_DIR.glob("*.md"):
                    try:
                        content = await self._read_text(user_file)
                        name_match = re.search(
                            r"\*\*Preferred Name:\*\*\s*(.*)", content, re.IGNORECASE
                        )
                        affinity_match = re.search(
                            r"\*\*Affinity Score:\*\*\s*(.*)", content, re.IGNORECASE
                        )
                        level_match = re.search(
                            r"\*\*Relationship Level:\*\*\s*(.*)",
                            content,
                            re.IGNORECASE,
                        )

                        relationships.append(
                            {
                                "id": user_file.stem,
                                "name": name_match.group(1).strip()
                                if name_match
                                else user_file.stem,
                                "affinity": int(affinity_match.group(1).strip())
                                if affinity_match
                                and affinity_match.group(1).strip().isdigit()
                                else 0,
                                "level": level_match.group(1).strip()
                                if level_match
                                else "Stranger",
                            }
                        )
                    except Exception as e:
                        logger.warning(
                            f"Error parsing user profile {user_file.name}: {e}"
                        )

            result["relationships"] = sorted(
                relationships, key=lambda x: x["affinity"], reverse=True
            )
            return result

        @self.app.put("/api/persona", dependencies=[Depends(self.verify_auth)])
        async def update_persona(data: dict):
            try:
                from pathlib import Path

                persona_dir = Path("persona")
                identity_file = persona_dir / "IDENTITY.md"
                mood_file = persona_dir / "MOOD.md"
                await self._write_text(identity_file, _build_identity_markdown(data))

                mood_value = data.get("mood")
                if mood_value:
                    tmp_mood = mood_file.with_suffix(".tmp")
                    await self._write_text(tmp_mood, mood_value)
                    await asyncio.to_thread(tmp_mood.replace, mood_file)

                logger.info(f"Persona updated: {data.get('name')}")
                return {"status": "success", "message": "Persona updated"}
            except Exception as e:
                logger.error(f"Error updating persona: {e}")
                return {"error": str(e)}

        @self.app.post("/api/persona/preview", dependencies=[Depends(self.verify_auth)])
        async def preview_persona(data: dict):
            try:
                from config import load_config
                from core.prompt import (
                    SOUL_FILE,
                    build_stable_system_prompt,
                    get_identity_data,
                )

                cfg = load_config()
                draft = data.get("persona") or {}
                if not isinstance(draft, dict):
                    return {"error": "persona must be an object"}

                channel = str(data.get("channel") or "web").strip().lower()
                if channel not in {"web", "discord", "telegram", "whatsapp"}:
                    return {"error": "Unsupported preview channel"}

                user_message = (
                    str(data.get("user_message") or "").strip()
                    or "Introduce yourself and explain how you would help me on this platform."
                )
                identity_content = _build_identity_markdown(draft)
                identity_data = get_identity_data(identity_content=identity_content)
                effective_style, style_source = _resolve_channel_style(identity_data, channel)
                soul_content = ""
                if SOUL_FILE.exists():
                    soul_content = SOUL_FILE.read_text(encoding="utf-8")
                system_prompt = build_stable_system_prompt(
                    sender_id="preview-user",
                    channel=channel,
                    chat_id="preview-chat",
                    model=cfg.llm.model,
                    allowed_paths=cfg.whitelist.allowed_paths,
                    skill_registry=getattr(self, "_skill_registry", None),
                    config=cfg,
                    soul=soul_content,
                    identity_raw=identity_content,
                    sender_name="Preview User",
                )

                provider = self.llm_client.resolve_provider(
                    cfg.llm.model, default_base_url=cfg.llm.base_url
                )
                preview_text = None
                preview_error = None
                try:
                    preview_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ]
                    response = await self.llm_client.complete(
                        provider,
                        ChatRequest(
                            messages=preview_messages,
                            max_tokens=180,
                            session_id="persona-preview",
                        ),
                    )
                    choices = getattr(response, "choices", None) or response.get("choices", [])
                    if choices:
                        message = choices[0].message if hasattr(choices[0], "message") else choices[0].get("message", {})
                        if hasattr(message, "content"):
                            preview_text = message.content
                        elif isinstance(message, dict):
                            preview_text = message.get("content")
                except Exception as preview_exc:
                    preview_error = str(preview_exc)

                excerpt = system_prompt
                if len(excerpt) > 1800:
                    excerpt = excerpt[:1800].rstrip() + "\n... [truncated]"

                return {
                    "channel": channel,
                    "model": cfg.llm.model,
                    "effective_style": effective_style,
                    "style_source": style_source,
                    "system_prompt_excerpt": excerpt,
                    "preview_text": preview_text,
                    "error": preview_error,
                }
            except Exception as e:
                logger.error(f"Error previewing persona: {e}")
                return {"error": str(e)}

        @self.app.get("/api/persona/export", dependencies=[Depends(self.verify_auth)])
        async def export_persona():
            try:
                from pathlib import Path

                root_dir = Path(__file__).parent.parent
                persona_dir = root_dir / "persona"
                identity_content = ""
                soul_content = ""
                if persona_dir.exists():
                    for item in persona_dir.iterdir():
                        if item.is_file():
                            if item.name.lower() == "identity.md":
                                identity_content = await self._read_text(item)
                            elif item.name.lower() == "soul.md":
                                soul_content = await self._read_text(item)
                export_data = (
                    "<!-- SECTION: IDENTITY -->\n"
                    f"{identity_content}\n\n"
                    "<!-- SECTION: SOUL -->\n"
                    f"{soul_content}\n"
                )
                logger.info(
                    f"Persona export: identity={len(identity_content)}ch, soul={len(soul_content)}ch"
                )
                return {"filename": "limebot_persona.md", "content": export_data}
            except Exception as e:
                logger.error(f"Error exporting persona: {e}")
                return {"error": str(e)}

        @self.app.post("/api/persona/import", dependencies=[Depends(self.verify_auth)])
        async def import_persona(data: dict):
            try:
                import re
                import shutil
                from pathlib import Path

                content = data.get("content", "")
                if not content:
                    raise ValueError("No content provided")

                identity_match = re.search(
                    r"<!-- SECTION: IDENTITY -->\s*(.*?)\s*(?=<!-- SECTION: SOUL -->|$)",
                    content,
                    re.DOTALL,
                )
                soul_match = re.search(
                    r"<!-- SECTION: SOUL -->\s*(.*)", content, re.DOTALL
                )

                if not identity_match and not soul_match:
                    raise ValueError(
                        "Invalid persona file format. Missing proper section headers."
                    )

                root_dir = Path(__file__).parent.parent
                persona_dir = root_dir / "persona"
                persona_dir.mkdir(exist_ok=True)
                timestamp = int(time.time())

                def safe_update(filename, new_content):
                    target_path = persona_dir / filename
                    existing_path = next(
                        (
                            item
                            for item in persona_dir.iterdir()
                            if item.name.lower() == filename.lower()
                        ),
                        None,
                    )
                    if existing_path and existing_path.exists():
                        shutil.copy(
                            existing_path,
                            existing_path.with_suffix(f".md.{timestamp}.bak"),
                        )
                        target_path = existing_path
                    target_path.write_text(new_content.strip() + "\n", encoding="utf-8")
                    return target_path.name

                updated_files = []
                if identity_match:
                    updated_files.append(await asyncio.to_thread(
                        safe_update, "IDENTITY.md", identity_match.group(1)
                    )
                    )
                if soul_match:
                    updated_files.append(
                        await asyncio.to_thread(
                            safe_update, "SOUL.md", soul_match.group(1)
                        )
                    )

                logger.info(f"Persona imported. Updated: {', '.join(updated_files)}")
                return {
                    "status": "success",
                    "message": f"Persona imported ({', '.join(updated_files)}). Backups created.",
                }
            except Exception as e:
                logger.error(f"Error importing persona: {e}")
                return {"error": str(e)}

        @self.app.get("/api/instances", dependencies=[Depends(self.verify_auth)])
        async def get_instances():
            sessions = self.session_manager.get_sessions()
            return list(sessions.values())

        @self.app.delete(
            "/api/instances/{instance_id}", dependencies=[Depends(self.verify_auth)]
        )
        async def delete_instance(instance_id: str):
            success = await self.session_manager.delete_session(instance_id)
            if success:
                return {
                    "status": "success",
                    "message": f"Instance {instance_id} deleted",
                }
            raise HTTPException(status_code=404, detail="Instance not found")

        @self.app.post(
            "/api/instances/delete-batch", dependencies=[Depends(self.verify_auth)]
        )
        async def delete_instances_batch(request: Request):
            data = await request.json()
            ids = data.get("ids", [])
            if not ids:
                return {"status": "success", "deleted": 0}

            count = await self.session_manager.delete_sessions(ids)
            return {
                "status": "success",
                "message": f"Deleted {count} instances",
                "deleted": count,
            }

        @self.app.get("/api/sessions", dependencies=[Depends(self.verify_auth)])
        async def get_sessions():
            return await get_instances()

        @self.app.get("/api/llm/models")
        async def get_llm_models():
            codex_status = get_codex_oauth_status()
            models = [
                # ── Google Gemini ─────────────────────────────────────────────
                {
                    "id": "gemini/gemini-3.6-flash",
                    "name": "Gemini 3.6 Flash",
                    "provider": "gemini",
                },
                {
                    "id": "gemini/gemini-3.5-flash",
                    "name": "Gemini 3.5 Flash",
                    "provider": "gemini",
                },
                {
                    "id": "gemini/gemini-3.5-flash-lite",
                    "name": "Gemini 3.5 Flash-Lite",
                    "provider": "gemini",
                },
                {
                    "id": "gemini/gemini-3.1-flash-lite",
                    "name": "Gemini 3.1 Flash-Lite",
                    "provider": "gemini",
                },
                {
                    "id": "gemini/gemini-3.1-pro-preview",
                    "name": "Gemini 3.1 Pro (Preview)",
                    "provider": "gemini",
                },
                {
                    "id": "gemini/gemini-3-flash-preview",
                    "name": "Gemini 3 Flash (Preview)",
                    "provider": "gemini",
                },
                # ── OpenAI ────────────────────────────────────────────────────
                *_OPENAI_CURATED_MODELS,
                # ── Anthropic ─────────────────────────────────────────────────
                {
                    "id": "anthropic/claude-fable-5",
                    "name": "Claude Fable 5",
                    "provider": "anthropic",
                },
                {
                    "id": "anthropic/claude-opus-5",
                    "name": "Claude Opus 5",
                    "provider": "anthropic",
                },
                {
                    "id": "anthropic/claude-sonnet-5",
                    "name": "Claude Sonnet 5",
                    "provider": "anthropic",
                },
                {
                    "id": "anthropic/claude-haiku-4-5-20251001",
                    "name": "Claude Haiku 4.5",
                    "provider": "anthropic",
                },
                # ── xAI Grok ─────────────────────────────────────────────────
                {
                    "id": "xai/grok-4.6",
                    "name": "Grok 4.6",
                    "provider": "xai",
                },
                {
                    "id": "xai/grok-4.6-latest",
                    "name": "Grok 4.6 (Latest Alias)",
                    "provider": "xai",
                },
                # ── DeepSeek ──────────────────────────────────────────────────
                {
                    "id": "deepseek/deepseek-v4-flash",
                    "name": "DeepSeek V4 Flash",
                    "provider": "deepseek",
                },
                {
                    "id": "deepseek/deepseek-v4-pro",
                    "name": "DeepSeek V4 Pro",
                    "provider": "deepseek",
                },
                {
                    "id": "deepseek/deepseek-v3.2",
                    "name": "DeepSeek V3.2",
                    "provider": "deepseek",
                },
                {
                    "id": "deepseek/deepseek-chat",
                    "name": "DeepSeek Chat (legacy)",
                    "provider": "deepseek",
                },
                {
                    "id": "deepseek/deepseek-reasoner",
                    "name": "DeepSeek Reasoner (legacy)",
                    "provider": "deepseek",
                },
                # ── Moonshot AI / Kimi ──────────────────────────────────────
                {
                    "id": "moonshot/kimi-k3",
                    "name": "Kimi K3",
                    "provider": "moonshot",
                },
                {
                    "id": "moonshot/kimi-k2.6",
                    "name": "Kimi K2.6",
                    "provider": "moonshot",
                },
                {
                    "id": "moonshot/kimi-k2-thinking",
                    "name": "Kimi K2 Thinking",
                    "provider": "moonshot",
                },
                {
                    "id": "moonshot/kimi-k2-instruct",
                    "name": "Kimi K2 Instruct",
                    "provider": "moonshot",
                },
                {
                    "id": "moonshot/kimi-k2.5",
                    "name": "Kimi K2.5",
                    "provider": "moonshot",
                },
                {
                    "id": "qwen/qwen3.5-plus",
                    "name": "Qwen 3.5 Plus",
                    "provider": "qwen",
                },
                {
                    "id": "qwen/qwen3.5-flash",
                    "name": "Qwen 3.5 Flash",
                    "provider": "qwen",
                },
                {
                    "id": "qwen/qwen3-max",
                    "name": "Qwen 3 Max",
                    "provider": "qwen",
                },
                {
                    "id": "qwen/qwen3.5-397b-a17b",
                    "name": "Qwen 3.5 397B",
                    "provider": "qwen",
                },
                # ── NVIDIA NIM (static fallbacks — dynamic list fetched below) ─
                {
                    "id": "nvidia/deepseek-ai/deepseek-v4-flash-0731",
                    "name": "DeepSeek V4 Flash (NVIDIA NIM)",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/deepseek-ai/deepseek-v4-pro",
                    "name": "DeepSeek V4 Pro (NVIDIA NIM)",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/qwen/qwen3-coder-next",
                    "name": "Qwen 3 Coder Next (NVIDIA NIM)",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/zai-org/glm-5",
                    "name": "GLM 5 (NVIDIA NIM)",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/openai/gpt-oss-120b",
                    "name": "GPT-OSS 120B",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/openai/gpt-oss-20b",
                    "name": "GPT-OSS 20B",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/z-ai/glm4.7",
                    "name": "GLM 4.7",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/moonshotai/kimi-k2-instruct",
                    "name": "Kimi K2 Instruct",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/moonshotai/kimi-k2-thinking",
                    "name": "Kimi K2 Thinking",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/moonshotai/kimi-k2.5",
                    "name": "Kimi K2.5",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/meta/llama-4-scout-17b-16e-instruct",
                    "name": "Llama 4 Scout",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/meta/llama-4-maverick-17b-128e-instruct",
                    "name": "Llama 4 Maverick",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/qwen/qwen3-next-80b-a3b-instruct",
                    "name": "Qwen 3 Next 80B",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/meta/llama-3.1-405b-instruct",
                    "name": "Llama 3.1 405B Instruct",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/meta/llama-3.3-70b-instruct",
                    "name": "Llama 3.3 70B Instruct",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/mistralai/mixtral-8x22b-instruct-v0.1",
                    "name": "Mixtral 8x22B Instruct",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/mistralai/mistral-large-2-instruct",
                    "name": "Mistral Large 2",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/deepseek-ai/deepseek-v3.2",
                    "name": "DeepSeek V3.2",
                    "provider": "nvidia",
                },
                {
                    "id": "nvidia/qwen/qwen3-next-80b-a3b-thinking",
                    "name": "Qwen 3 Next 80B Thinking",
                    "provider": "nvidia",
                },
            ]

            if codex_status.get("configured"):
                codex_models = _filter_supported_codex_models(
                    _load_piai_provider_models("openai-codex")
                )
                models.extend(codex_models or [dict(model) for model in _CODEX_FALLBACK_MODELS])

            from core.llm_utils import (
                OPENROUTER_CURATED_MODEL_IDS,
                fetch_openai_compatible_models,
                fetch_gemini_models,
                fetch_anthropic_models,
            )

            def openrouter_display_name(model_id: str) -> str:
                family, _, model = model_id.partition("/")
                label = model.replace("-", " ").replace(".", ".").title()
                provider_label = {
                    "x-ai": "xAI",
                    "z-ai": "Z.ai",
                    "qwen": "Qwen",
                    "openai": "OpenAI",
                    "moonshotai": "Moonshot AI",
                    "google": "Google",
                    "meta-llama": "Meta Llama",
                    "anthropic": "Anthropic",
                }.get(family, family.replace("-", " ").title())
                return f"{provider_label} {label}".strip()

            models.extend(
                {
                    "id": f"openrouter/{model_id}",
                    "name": openrouter_display_name(model_id),
                    "provider": "openrouter",
                }
                for model_id in OPENROUTER_CURATED_MODEL_IDS
            )

            api_keys = {
                "gemini": os.getenv("GEMINI_API_KEY"),
                "nvidia": os.getenv("NVIDIA_API_KEY"),
                "xai": os.getenv("XAI_API_KEY"),
                "anthropic": os.getenv("ANTHROPIC_API_KEY"),
                "deepseek": os.getenv("DEEPSEEK_API_KEY"),
                "openai": os.getenv("OPENAI_API_KEY"),
                "openrouter": os.getenv("OPENROUTER_API_KEY"),
                "moonshot": os.getenv("MOONSHOT_API_KEY")
                or os.getenv("MOONSHOTAI_API_KEY")
                or os.getenv("KIMI_API_KEY"),
                "qwen": os.getenv("DASHSCOPE_API_KEY"),
            }

            current_time = time.time()

            async def update_provider_cache(provider, fetch_func, *args):
                last = self._provider_models_last_update.get(provider, 0)
                if current_time - last > 3600:
                    try:
                        fetched = await fetch_func(*args)
                        if fetched:
                            self._provider_models_cache[provider] = fetched
                            self._provider_models_last_update[provider] = current_time
                            logger.info(
                                f"Updated model cache for {provider}: {len(fetched)} models"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to update {provider} models: {e}")

            if api_keys["gemini"]:
                await update_provider_cache(
                    "gemini",
                    fetch_gemini_models,
                    api_keys["gemini"],
                )
            if api_keys["openrouter"]:
                await update_provider_cache(
                    "openrouter",
                    fetch_openai_compatible_models,
                    api_keys["openrouter"],
                    os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
                    "openrouter",
                    True,
                )
            if api_keys["nvidia"]:
                await update_provider_cache(
                    "nvidia",
                    fetch_openai_compatible_models,
                    api_keys["nvidia"],
                    "https://integrate.api.nvidia.com/v1",
                    "nvidia",
                    True,
                )
            if api_keys["xai"]:
                await update_provider_cache(
                    "xai",
                    fetch_openai_compatible_models,
                    api_keys["xai"],
                    "https://api.x.ai/v1",
                    "xai",
                    True,
                )
            if api_keys["anthropic"]:
                await update_provider_cache(
                    "anthropic",
                    fetch_anthropic_models,
                    api_keys["anthropic"],
                )
            if api_keys["qwen"]:
                await update_provider_cache(
                    "qwen",
                    fetch_openai_compatible_models,
                    api_keys["qwen"],
                    os.getenv("LLM_BASE_URL")
                    or os.getenv("DASHSCOPE_BASE_URL")
                    or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    "qwen",
                    True,
                )
            if api_keys["openai"]:
                await update_provider_cache(
                    "openai",
                    fetch_openai_compatible_models,
                    api_keys["openai"],
                    "https://api.openai.com/v1",
                    "openai",
                    True,
                )
            if api_keys["moonshot"]:
                await update_provider_cache(
                    "moonshot",
                    fetch_openai_compatible_models,
                    api_keys["moonshot"],
                    os.getenv("MOONSHOT_BASE_URL")
                    or os.getenv("MOONSHOTAI_BASE_URL")
                    or "https://api.moonshot.ai/v1",
                    "moonshot",
                    True,
                )
            if api_keys["deepseek"]:
                await update_provider_cache(
                    "deepseek",
                    fetch_openai_compatible_models,
                    api_keys["deepseek"],
                    "https://api.deepseek.com",
                    "deepseek",
                    True,
                )

            existing_ids = {m["id"] for m in models}
            for cached_models in self._provider_models_cache.values():
                for cm in cached_models:
                    if cm["id"] not in existing_ids:
                        models.append(cm)
                        existing_ids.add(cm["id"])

            return {"models": models, "codexAuth": codex_status}

        @self.app.get("/api/llm/health", dependencies=[Depends(self.verify_auth)])
        async def check_llm_health():
            from config import load_config

            cfg = load_config()
            model = cfg.llm.model
            start = time.time()
            runtime = (
                self.agent.get_llm_runtime_status()
                if getattr(self, "agent", None)
                and hasattr(self.agent, "get_llm_runtime_status")
                else {
                    "configured_model": model,
                    "active_model": model,
                    "fallback_models": getattr(cfg.llm, "fallback_models", []),
                    "using_fallback": False,
                }
            )
            try:
                provider = self.llm_client.resolve_provider(
                    model, default_base_url=cfg.llm.base_url
                )
                await self.llm_client.complete(
                    provider,
                    ChatRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        max_tokens=16,
                        session_id="llm-health",
                    )
                )
                latency = int((time.time() - start) * 1000)
                return {
                    "status": "Healthy",
                    "latency_ms": latency,
                    "model": model,
                    "quota_remaining": "Unknown",
                    **runtime,
                }
            except Exception as e:
                error_msg = str(e)
                health_status = "Quota Exceeded" if "429" in error_msg else "Error"
                return {
                    "status": health_status,
                    "latency_ms": int((time.time() - start) * 1000),
                    "model": model,
                    "error": error_msg,
                    **runtime,
                }

        @self.app.get("/api/llm/runtime", dependencies=[Depends(self.verify_auth)])
        async def get_llm_runtime():
            from config import load_config

            cfg = load_config()
            if getattr(self, "agent", None) and hasattr(
                self.agent, "get_llm_runtime_status"
            ):
                return self.agent.get_llm_runtime_status()

            return {
                "configured_model": cfg.llm.model,
                "active_model": cfg.llm.model,
                "fallback_models": getattr(cfg.llm, "fallback_models", []),
                "using_fallback": False,
            }

        @self.app.get("/api/config", dependencies=[Depends(self.verify_auth)])
        async def get_config():
            from config import load_config

            cfg = load_config()
            env_dict = {
                "LLM_MODEL": cfg.llm.model,
                "DISCORD_ALLOW_FROM": ",".join(cfg.discord.allow_from),
                "DISCORD_ALLOW_CHANNELS": ",".join(cfg.discord.allow_channels),
                "DISCORD_ACTIVITY_TYPE": cfg.discord.activity_type,
                "DISCORD_ACTIVITY_TEXT": cfg.discord.activity_text,
                "DISCORD_STATUS": cfg.discord.status,
                "ENABLE_TELEGRAM": str(cfg.telegram.enabled).lower(),
                "TELEGRAM_API_BASE": cfg.telegram.api_base,
                "TELEGRAM_ALLOW_FROM": ",".join(cfg.telegram.allow_from),
                "TELEGRAM_ALLOW_CHATS": ",".join(cfg.telegram.allow_chats),
                "TELEGRAM_POLL_TIMEOUT": str(cfg.telegram.poll_timeout),
                "WHATSAPP_ALLOW_FROM": ",".join(cfg.whatsapp.allow_from),
                "ENABLE_WHATSAPP": str(cfg.whatsapp.enabled).lower(),
                "WHATSAPP_BRIDGE_URL": cfg.whatsapp.bridge_url,
                "ALLOWED_PATHS": cfg.whitelist.allowed_paths,
                "APP_API_KEY": cfg.whitelist.api_key,
                "ENABLE_DYNAMIC_PERSONALITY": str(
                    getattr(cfg.llm, "enable_dynamic_personality", False)
                ).lower(),
                "VIDEO_WHISPER_ENABLED": str(
                    getattr(getattr(cfg, "video", None), "whisper_enabled", False)
                ).lower(),
                "MAX_ITERATIONS": str(getattr(cfg, "max_iterations", 30)),
                "COMMAND_TIMEOUT": str(getattr(cfg, "command_timeout", 0)),
                "RUN_COMMAND_MAX_SECONDS": str(
                    getattr(cfg, "run_command_max_seconds", 180)
                ),
                "STALL_TIMEOUT": str(getattr(cfg, "stall_timeout", 0)),
                "PERSONALITY_WHITELIST": ",".join(
                    getattr(cfg, "personality_whitelist", [])
                ),
                "APPROVAL_POLICY_PROFILE": getattr(cfg, "approval_policy_profile", ""),
                "AUTONOMOUS_MODE": str(getattr(cfg, "autonomous_mode", False)).lower(),
                "ALLOW_UNSAFE_COMMANDS": str(
                    getattr(cfg, "allow_unsafe_commands", False)
                ).lower(),
                "LIMEBOT_ENABLE_TOOL_SHORTLIST": str(
                    getattr(cfg, "tool_shortlist_enabled", False)
                ).lower(),
                "WEB_PORT": str(getattr(cfg.web, "port", 8000)),
                "WEB_ALLOWED_ORIGINS": ",".join(
                    getattr(cfg.web, "allowed_origins", [])
                ),
                "LLM_PROXY_URL": getattr(cfg.llm, "proxy_url", ""),
                "BROWSER_MODE": getattr(cfg.browser, "mode", "isolated"),
                "BROWSER_CHANNEL": getattr(cfg.browser, "channel", ""),
                "BROWSER_CDP_URL": getattr(cfg.browser, "cdp_url", ""),
                "BROWSER_USER_DATA_DIR": getattr(cfg.browser, "user_data_dir", ""),
                "BROWSER_PROFILE_DIRECTORY": getattr(
                    cfg.browser, "profile_directory", ""
                ),
                "SEARCH_PROVIDER": getattr(
                    getattr(cfg, "search", None), "provider", "auto"
                ),
            }
            secret_dict = {
                "APP_API_KEY": _serialize_secret(cfg.whitelist.api_key or ""),
                "GEMINI_API_KEY": _serialize_secret(os.getenv("GEMINI_API_KEY", "")),
                "OPENAI_API_KEY": _serialize_secret(os.getenv("OPENAI_API_KEY", "")),
                "OPENROUTER_API_KEY": _serialize_secret(os.getenv("OPENROUTER_API_KEY", "")),
                "ANTHROPIC_API_KEY": _serialize_secret(os.getenv("ANTHROPIC_API_KEY", "")),
                "XAI_API_KEY": _serialize_secret(os.getenv("XAI_API_KEY", "")),
                "DEEPSEEK_API_KEY": _serialize_secret(os.getenv("DEEPSEEK_API_KEY", "")),
                "MOONSHOT_API_KEY": _serialize_secret(
                    os.getenv("MOONSHOT_API_KEY", "")
                    or os.getenv("MOONSHOTAI_API_KEY", "")
                    or os.getenv("KIMI_API_KEY", "")
                ),
                "NVIDIA_API_KEY": _serialize_secret(os.getenv("NVIDIA_API_KEY", "")),
                "DASHSCOPE_API_KEY": _serialize_secret(os.getenv("DASHSCOPE_API_KEY", "")),
                "TAVILY_API_KEY": _serialize_secret(os.getenv("TAVILY_API_KEY", "")),
                "BRAVE_SEARCH_API_KEY": _serialize_secret(
                    os.getenv("BRAVE_SEARCH_API_KEY", "") or os.getenv("BRAVE_API_KEY", "")
                ),
                "SERPAPI_API_KEY": _serialize_secret(
                    os.getenv("SERPAPI_API_KEY", "") or os.getenv("SERPAPI_KEY", "")
                ),
                "ELEVENLABS_API_KEY": _serialize_secret(
                    os.getenv("ELEVENLABS_API_KEY", "")
                ),
                "DISCORD_TOKEN": _serialize_secret(cfg.discord.token or ""),
                "TELEGRAM_BOT_TOKEN": _serialize_secret(cfg.telegram.token or ""),
            }
            for key in [
                "LLM_BASE_URL",
                "LLM_PROXY_URL",
            ]:
                val = os.getenv(key)
                if val:
                    env_dict[key] = val
            return {"env": env_dict, "secrets": secret_dict}

        @self.app.get("/api/auth/codex/status", dependencies=[Depends(self.verify_auth)])
        async def get_codex_auth_status():
            from config import load_config

            cfg = load_config()
            status_payload = get_codex_oauth_status()
            status_payload["selected_model_uses_codex_auth"] = str(
                getattr(cfg.llm, "model", "") or ""
            ).startswith("openai-codex/")
            return status_payload

        @self.app.get("/api/discord/config", dependencies=[Depends(self.verify_auth)])
        async def get_discord_config():
            from pathlib import Path

            cfg_path = get_config_file()
            if not cfg_path.exists():
                return {"discord": {}}
            try:
                data = await self._read_json(cfg_path, default={})
            except Exception as e:
                logger.error(f"Error reading limebot.json: {e}")
                return {"discord": {}}
            return {"discord": data.get("discord", {})}

        @self.app.post("/api/discord/config", dependencies=[Depends(self.verify_auth)])
        async def update_discord_config(payload: dict):
            from pathlib import Path

            cfg_path = get_config_file()
            try:
                data = {}
                if cfg_path.exists():
                    data = await self._read_json(cfg_path, default={})
                discord_cfg = payload.get("discord")
                if not isinstance(discord_cfg, dict):
                    return {"error": "discord config must be an object"}
                data["discord"] = discord_cfg
                await self._write_json(cfg_path, data)
                logger.info("Discord UI config saved. Restarting...")
                asyncio.get_running_loop().call_later(1.0, _spawn_restart)
                return {
                    "status": "updated",
                    "message": "Discord configuration saved. Restarting...",
                }
            except Exception as e:
                logger.error(f"Error saving discord config: {e}")
                return {"error": str(e)}

        @self.app.post("/api/config", dependencies=[Depends(self.verify_auth)])
        async def update_config(data: dict):
            """Update .env file and restart to apply changes."""
            try:
                from pathlib import Path

                env_file = get_env_file()
                new_env = data.get("env", {})
                clear_secrets = {
                    str(key).strip()
                    for key in data.get("clear_secrets", []) or []
                    if str(key).strip()
                }
                cfg_json_file = get_config_file()

                if not isinstance(new_env, dict):
                    return {"error": "env must be an object"}

                if "LLM_MODEL" in new_env:
                    model_value = str(new_env["LLM_MODEL"] or "").strip()
                    if not model_value:
                        model_value = "ollama/llama3"
                        logger.warning(
                            "Received empty LLM_MODEL from config UI; using ollama/llama3."
                        )
                    new_env["LLM_MODEL"] = model_value

                    try:
                        cfg_json = (
                            await self._read_json(cfg_json_file, default={})
                            if cfg_json_file.exists()
                            else {}
                        )
                        if not isinstance(cfg_json, dict):
                            cfg_json = {}
                        llm_cfg = cfg_json.get("llm")
                        if isinstance(llm_cfg, dict) and "model" in llm_cfg:
                            llm_cfg.pop("model", None)
                            if llm_cfg:
                                cfg_json["llm"] = llm_cfg
                            else:
                                cfg_json.pop("llm", None)
                            await self._write_json(cfg_json_file, cfg_json)
                    except Exception as e:
                        logger.warning(
                            f"Failed to remove deprecated llm.model from limebot.json: {e}"
                        )

                if "ALLOWED_PATHS" in new_env:
                    paths_data = new_env.pop("ALLOWED_PATHS")
                    paths_file = get_allowed_paths_file()
                    if isinstance(paths_data, list):
                        paths = [str(p).strip() for p in paths_data if str(p).strip()]
                    else:
                        paths = [
                            p.strip() for p in str(paths_data).split(",") if p.strip()
                        ]
                    await self._write_text(paths_file, "\n".join(paths))

                current_lines = (
                    (await self._read_text(env_file)).splitlines()
                    if env_file.exists()
                    else []
                )
                moonshot_aliases_to_clear = (
                    set(_MOONSHOT_ENV_ALIASES)
                    if "MOONSHOT_API_KEY" in new_env or "MOONSHOT_API_KEY" in clear_secrets
                    else set()
                )
                final_lines = []
                processed_keys: set[str] = set()

                for line in current_lines:
                    stripped = line.strip()
                    if "=" in stripped and not stripped.startswith("#"):
                        key = stripped.split("=", 1)[0].strip()
                        if key == "ALLOWED_PATHS":
                            continue
                        if key in new_env:
                            final_lines.append(f"{key}={new_env[key]}")
                            processed_keys.add(key)
                        elif key in moonshot_aliases_to_clear:
                            final_lines.append(f"{key}=")
                            processed_keys.add(key)
                        elif key in clear_secrets and key in _SECRET_CONFIG_KEYS:
                            final_lines.append(f"{key}=")
                            processed_keys.add(key)
                        else:
                            final_lines.append(line)
                    else:
                        final_lines.append(line)

                for key, val in new_env.items():
                    if key not in processed_keys:
                        final_lines.append(f"{key}={val}")

                for key in clear_secrets:
                    if key in _SECRET_CONFIG_KEYS and key not in processed_keys:
                        final_lines.append(f"{key}=")

                await self._write_text(env_file, "\n".join(final_lines))

                # Update os.environ so the child process inherits
                # the correct values (load_dotenv uses override=False,
                # so it won't re-read .env values that already exist
                # in the inherited environment).
                for key, val in new_env.items():
                    os.environ[key] = str(val)
                for key in moonshot_aliases_to_clear:
                    os.environ[key] = ""
                for key in clear_secrets:
                    if key in _SECRET_CONFIG_KEYS:
                        os.environ[key] = ""

                # Clear the cached config so it's rebuilt on next access
                from config import reload_config

                reload_config()

                logger.info("Configuration saved. Restarting...")
                asyncio.get_running_loop().call_later(1.0, _spawn_restart)
                return {
                    "status": "updated",
                    "message": "Configuration saved. Restarting...",
                }
            except Exception as e:
                logger.error(f"Error updating config: {e}")
                return {"error": str(e)}

        @self.app.get("/api/mcp/config", dependencies=[Depends(self.verify_auth)])
        async def get_mcp_config():
            from core.mcp_client import CONFIG_PATH
            import json

            if not CONFIG_PATH.exists():
                return {"mcpServers": {}}
            return await self._read_json(CONFIG_PATH, default={"mcpServers": {}})

        @self.app.post("/api/mcp/config", dependencies=[Depends(self.verify_auth)])
        async def update_mcp_config(data: dict):
            from core.mcp_client import (
                CONFIG_PATH,
                get_mcp_manager,
                validate_mcp_config,
            )
            import json

            try:
                ok, err = validate_mcp_config(data)
                if not ok:
                    return {"error": err}
                await self._write_json(CONFIG_PATH, data)
                # Re-initialize MCP manager to apply changes
                mcp_manager = get_mcp_manager()
                asyncio.create_task(mcp_manager.initialize())
                return {"status": "success", "message": "MCP configuration updated"}
            except Exception as e:
                logger.error(f"Error updating MCP config: {e}")
                return {"error": str(e)}

        @self.app.get("/api/mcp/status", dependencies=[Depends(self.verify_auth)])
        async def get_mcp_status():
            from core.mcp_client import get_mcp_manager

            manager = get_mcp_manager()
            return {"status": manager.get_status()}

        @self.app.get("/api/setup/status")
        async def get_setup_status(restart_token: Optional[str] = None):
            try:
                from config import load_config
                from core.llm_utils import get_api_key_for_model
                from core.prompt import get_setup_state

                cfg = load_config()
                model = cfg.llm.model
                api_key = get_api_key_for_model(model)
                persona_state = get_setup_state()

                is_local = (
                    model and ("ollama" in model or "local" in model)
                ) or cfg.llm.base_url

                missing = []
                if not model:
                    missing.append("LLM_MODEL")
                if not api_key and not is_local:
                    missing.append("API_KEY")

                restart_recognized = False
                if restart_token and _SETUP_STATE_PATH.exists():
                    try:
                        saved_state = json.loads(_SETUP_STATE_PATH.read_text(encoding="utf-8"))
                        saved_token = saved_state.get("restart_token")
                        if saved_token and restart_token == saved_token:
                            restart_recognized = True
                    except Exception as e:
                        logger.warning(f"Error reading setup state JSON: {e}")

                return {
                    "configured": len(missing) == 0,
                    "missing_keys": missing,
                    "persona_ready": persona_state["complete"],
                    "persona_missing": persona_state["missing"],
                    "setup_required": len(missing) > 0 or not persona_state["complete"],
                    "auth_required": self._is_auth_required(),
                    "restart_recognized": restart_recognized,
                    "boot_id": self._boot_id,
                }
            except Exception as e:
                logger.error(f"Error checking setup status: {e}")
                return {"configured": False, "error": str(e)}

        @self.app.get("/api/setup/tailscale")
        async def get_tailscale_status():
            try:
                import socket
                import psutil

                interfaces = []
                tailscale_ip = None
                for interface, addrs in psutil.net_if_addrs().items():
                    for addr in addrs:
                        if addr.family == socket.AF_INET:
                            is_tailscale = "Tailscale" in interface or (
                                addr.address.startswith("100.")
                                and not addr.address.startswith("100.64")
                            )
                            if is_tailscale:
                                tailscale_ip = addr.address
                            interfaces.append(
                                {
                                    "name": interface,
                                    "ip": addr.address,
                                    "is_tailscale": is_tailscale,
                                }
                            )
                return {
                    "interfaces": interfaces,
                    "tailscale_ip": tailscale_ip,
                    "has_tailscale": tailscale_ip is not None,
                }
            except Exception as e:
                logger.error(f"Error checking tailscale: {e}")
                return {"error": str(e)}

        @self.app.get("/api/stats", dependencies=[Depends(self.verify_auth)])
        async def get_stats():
            uptime = int(time.time() - self.start_time)
            channel_stats = []
            for ch in self.channels:
                ch_status = "Connected"
                if hasattr(ch, "get_status"):
                    status_data = ch.get_status()
                    ch_status = status_data.get("status", ch_status).replace("_", " ").title()
                if hasattr(ch, "client") and hasattr(ch.client, "is_ready"):
                    if not ch.client.is_ready():
                        ch_status = "Connecting..."
                channel_stats.append(
                    {
                        "name": ch.name,
                        "type": ch.__class__.__name__,
                        "status": ch_status,
                    }
                )

            sessions = self.session_manager.get_sessions()
            subagents = [s for s in sessions.values() if s.get("parent_id")]
            return {
                "uptime": uptime,
                "gateway_url": f"ws://localhost:{self.actual_port}/ws",
                "channels": channel_stats,
                "sessions": len(sessions),
                "sessions_count": len(sessions),
                "instances_count": len(
                    [s for s in sessions.values() if not s.get("parent_id")]
                ),
                "subagents_count": len(subagents),
                "cron_status": "Enabled",
            }

        @self.app.get("/api/metrics", dependencies=[Depends(self.verify_auth)])
        async def get_metrics():
            from core.metrics import MetricsCollector

            return MetricsCollector().get_snapshot()

        @self.app.get("/api/logs", dependencies=[Depends(self.verify_auth)])
        async def get_logs(lines: int = 100):
            """Return the last N lines of logs."""
            try:
                from pathlib import Path

                log_file = Path("logs/limebot.log")
                if not log_file.exists():
                    return {"logs": ["No logs found."]}
                return {"logs": await self._tail_log_file(log_file, lines)}
            except Exception as e:
                logger.error(f"Error reading logs: {e}")
                return {"logs": [f"Error reading logs: {e}"]}

        @self.app.get("/api/memory", dependencies=[Depends(self.verify_auth)])
        async def get_memory():
            from core.vectors import get_vector_service

            vector_service = get_vector_service()
            async def _markdown_fallback(reason: str):
                notice = (
                    "Vector index is empty; showing Markdown memory files."
                    if reason == "vector_index_empty"
                    else "Using Markdown memory files as the fallback."
                )
                try:
                    memories = await vector_service.get_markdown_entries(limit=500)
                    return {
                        "enabled": False,
                        "mode": "grep_fallback",
                        "read_only": True,
                        "notice": notice,
                        "reason": reason,
                        "memories": memories,
                    }
                except Exception as e:
                    logger.error(f"Error reading fallback memory: {e}")
                    return {
                        "enabled": False,
                        "mode": "grep_fallback",
                        "read_only": True,
                        "notice": notice,
                        "reason": reason,
                        "error": str(e),
                        "memories": [],
                    }

            if not vector_service.is_enabled:
                return await _markdown_fallback("semantic_embeddings_unavailable")

            try:
                memories = await vector_service.get_all(limit=100)
                if not memories:
                    # A configured embedding provider does not imply that the
                    # LanceDB table has entries.  Keep the explorer useful
                    # while the Markdown source remains the durable record.
                    return await _markdown_fallback("vector_index_empty")
                return {
                    "enabled": True,
                    "mode": "vector",
                    "read_only": False,
                    "memories": memories,
                }
            except Exception as e:
                logger.error(f"Error reading memory: {e}")
                return await _markdown_fallback("vector_index_error")

        @self.app.get("/api/memory/debug", dependencies=[Depends(self.verify_auth)])
        async def get_memory_debug(session_key: Optional[str] = None, limit: int = 20):
            if not getattr(self, "agent", None):
                return {"traces": []}
            try:
                limit = max(1, min(int(limit), 100))
            except Exception:
                limit = 20
            return {
                "traces": self.agent.get_recent_rag_traces(
                    session_key=session_key, limit=limit
                )
                or []
            }

        @self.app.delete(
            "/api/memory/{entry_id}", dependencies=[Depends(self.verify_auth)]
        )
        async def delete_memory(entry_id: str):
            from core.vectors import get_vector_service

            vector_service = get_vector_service()
            if not vector_service.is_enabled:
                raise HTTPException(status_code=400, detail="Memory system disabled")

            success = await vector_service.delete_entry(entry_id)
            if success:
                return {"status": "success", "message": f"Memory {entry_id} deleted"}
            raise HTTPException(status_code=500, detail="Failed to delete memory")

        @self.app.get("/api/skills", dependencies=[Depends(self.verify_auth)])
        async def list_skills():
            from core.skill_installer import SkillInstaller

            return SkillInstaller().list_skills()

        @self.app.get(
            "/api/capabilities/resolve",
            dependencies=[Depends(self.verify_auth)],
        )
        async def resolve_capabilities(
            text: str = "", session_key: Optional[str] = None
        ):
            """Explain how a task resolves against the live capability snapshot."""
            agent = getattr(self, "agent", None)
            if not agent or not hasattr(agent, "resolve_capabilities"):
                raise HTTPException(status_code=503, detail="Agent not initialized")
            return agent.resolve_capabilities(
                text=str(text or ""),
                session_key=str(session_key or "").strip() or None,
            )

        def _get_subagent_registry():
            from core.subagents import SubagentRegistry

            if hasattr(self, "agent") and self.agent:
                return self.agent.subagent_registry
            return SubagentRegistry()

        async def _load_subagent_registry(refresh_tools: bool = True):
            registry = _get_subagent_registry()
            await asyncio.to_thread(registry.discover_and_load)
            if (
                refresh_tools
                and hasattr(self, "agent")
                and self.agent
                and hasattr(self.agent, "_refresh_tool_definitions")
            ):
                self.agent._refresh_tool_definitions()
            return registry

        async def _reload_subagents() -> tuple[Any, list[dict]]:
            registry = await _load_subagent_registry()
            return registry, registry.list_definitions()

        @self.app.get("/api/subagents", dependencies=[Depends(self.verify_auth)])
        async def list_subagents():
            registry = _get_subagent_registry()
            if not registry.subagents:
                await asyncio.to_thread(registry.discover_and_load)
            return {
                "subagents": registry.list_definitions(),
                "location_options": registry.get_location_options(),
                "default_selection": registry.get_default_selection(),
                "selection_options": registry.get_selector_options(),
            }

        @self.app.post("/api/subagents", dependencies=[Depends(self.verify_auth)])
        async def create_subagent(request: Request):
            body = await request.json()
            registry = _get_subagent_registry()
            try:
                saved = await asyncio.to_thread(
                    registry.save_subagent,
                    name=body.get("name", ""),
                    description=body.get("description", ""),
                    prompt=body.get("prompt", ""),
                    tools=body.get("tools"),
                    disallowed_tools=body.get("disallowed_tools"),
                    model=body.get("model", "inherit"),
                    max_turns=body.get("max_turns"),
                    background=body.get("background", False),
                    location=body.get("location", "project_limebot"),
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            registry, subagents = await _reload_subagents()
            return {
                "status": "success",
                "subagent": saved,
                "subagents": subagents,
                "location_options": registry.get_location_options(),
                "default_selection": registry.get_default_selection(),
                "selection_options": registry.get_selector_options(),
            }

        @self.app.put("/api/subagents/settings", dependencies=[Depends(self.verify_auth)])
        async def update_subagent_settings(request: Request):
            registry = await _load_subagent_registry(refresh_tools=False)
            body = await request.json()
            selection = body.get("default_selection", "auto")
            try:
                saved_selection = await asyncio.to_thread(
                    registry.set_default_selection, selection
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return {
                "status": "success",
                "default_selection": saved_selection,
                "selection_options": registry.get_selector_options(),
            }

        @self.app.put(
            "/api/subagents/{subagent_id:path}",
            dependencies=[Depends(self.verify_auth)],
        )
        async def update_subagent(subagent_id: str, request: Request):
            body = await request.json()
            registry = _get_subagent_registry()
            try:
                saved = await asyncio.to_thread(
                    registry.save_subagent,
                    name=body.get("name", ""),
                    description=body.get("description", ""),
                    prompt=body.get("prompt", ""),
                    tools=body.get("tools"),
                    disallowed_tools=body.get("disallowed_tools"),
                    model=body.get("model", "inherit"),
                    max_turns=body.get("max_turns"),
                    background=body.get("background", False),
                    location=body.get("location", "project_limebot"),
                    subagent_id=subagent_id,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            registry, subagents = await _reload_subagents()
            return {
                "status": "success",
                "subagent": saved,
                "subagents": subagents,
                "location_options": registry.get_location_options(),
                "default_selection": registry.get_default_selection(),
                "selection_options": registry.get_selector_options(),
            }

        @self.app.delete(
            "/api/subagents/{subagent_id:path}",
            dependencies=[Depends(self.verify_auth)],
        )
        async def delete_subagent(subagent_id: str):
            registry = _get_subagent_registry()
            try:
                await asyncio.to_thread(registry.delete_subagent, subagent_id)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            registry, subagents = await _reload_subagents()
            return {
                "status": "success",
                "subagents": subagents,
                "location_options": registry.get_location_options(),
                "default_selection": registry.get_default_selection(),
                "selection_options": registry.get_selector_options(),
            }

        @self.app.post("/api/skills/install", dependencies=[Depends(self.verify_auth)])
        async def install_skill(request: Request):
            from core.skill_installer import SkillInstaller

            body = await request.json()
            repo_url = body.get("repo_url", "")
            if not repo_url:
                return {"status": "error", "message": "repo_url is required"}
            return SkillInstaller().install(
                repo_url, ref=body.get("ref", "main"), name=body.get("name")
            )

        @self.app.delete(
            "/api/skills/{skill_name}", dependencies=[Depends(self.verify_auth)]
        )
        async def uninstall_skill(skill_name: str, force: bool = False):
            from core.skill_installer import SkillInstaller

            return SkillInstaller().uninstall(skill_name, force=force)

        @self.app.post(
            "/api/skills/{skill_name}/update", dependencies=[Depends(self.verify_auth)]
        )
        async def update_skill(skill_name: str):
            from core.skill_installer import SkillInstaller

            return SkillInstaller().update(skill_name)

        @self.app.post(
            "/api/skills/{skill_name}/deps", dependencies=[Depends(self.verify_auth)]
        )
        async def install_skill_deps(skill_name: str):
            from core.skill_installer import SkillInstaller

            return SkillInstaller().install_skill_deps(skill_name)

        @self.app.post(
            "/api/skills/{skill_name}/toggle", dependencies=[Depends(self.verify_auth)]
        )
        async def toggle_skill(skill_name: str, request: Request):
            """Enable or disable a skill. Triggers a restart to apply changes."""
            from core.skill_installer import SkillInstaller

            body = await request.json()
            installer = SkillInstaller()
            result = (
                installer.enable(skill_name)
                if body.get("enable")
                else installer.disable(skill_name)
            )
            if result.get("status") == "success":
                logger.info(
                    f"Skill '{skill_name}' toggled to {body.get('enable')}. Restarting..."
                )
                asyncio.get_running_loop().call_later(1.0, _spawn_restart)
            return result

        @self.app.post("/api/notify", dependencies=[Depends(self.verify_auth)])
        async def notify(request: Request):
            """
            Send a notification to one or more channels (web/discord).
            Payload:
              - channels: "web" | "discord" | ["web","discord"]
              - content: message text
              - web_chat_id: optional (default "system")
              - discord_channel_ids: optional list or single id
              - kind: optional string (e.g., "github_pr")
              - data: optional object for structured notifications
            """
            data = await request.json()
            channels = data.get("channels") or []
            if isinstance(channels, str):
                channels = [channels]
            content = data.get("content", "").strip()
            if not content:
                raise HTTPException(status_code=400, detail="content is required")

            web_chat_id = data.get("web_chat_id", "system")
            discord_ids = data.get("discord_channel_ids") or []
            if isinstance(discord_ids, str):
                discord_ids = [discord_ids]

            meta = {
                "type": "notification",
                "kind": data.get("kind"),
                "data": data.get("data"),
            }

            if "web" in channels:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel="web",
                        chat_id=web_chat_id,
                        content=content,
                        metadata=meta,
                    )
                )

            if "discord" in channels and discord_ids:
                for chan_id in discord_ids:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel="discord",
                            chat_id=str(chan_id),
                            content=content,
                            metadata=meta,
                        )
                    )

            return {"status": "success", "channels": channels}

        @self.app.post("/api/skill/{skill_name}/{action}")
        async def skill_api(skill_name: str, action: str, request: Request):
            from core.skills import SkillAPI, SkillRegistry

            try:
                data = await request.json()
            except Exception:
                data = {}
            if not hasattr(self, "_skill_api"):
                self._skill_registry = SkillRegistry(
                    skill_dirs=[str(path) for path in get_skill_dirs()], config={"skills": {"entries": {}}}
                )
                self._skill_registry.discover_and_load()
                self._skill_api = SkillAPI(
                    registry=self._skill_registry, bus=self.bus, channels=self.channels
                )
            return await self._skill_api.handle_request(
                skill_name=skill_name, action=action, data=data or {}
            )

        @self.app.post("/api/control/restart", dependencies=[Depends(self.verify_auth)])
        async def restart_backend():
            logger.warning("Restart requested via API...")

            async def _restart():
                await asyncio.sleep(1)
                os.environ["LIMEBOT_SOFT_RESTART"] = "1"
                os.execl(os.sys.executable, os.sys.executable, *os.sys.argv)

            asyncio.create_task(_restart())
            return {"status": "restarting", "message": "Backend is restarting..."}

        @self.app.post(
            "/api/control/clear-cache", dependencies=[Depends(self.verify_auth)]
        )
        async def clear_cache():
            try:
                cleared = []
                if hasattr(self, "_provider_models_cache"):
                    self._provider_models_cache.clear()
                    self._provider_models_last_update.clear()
                    cleared.append("provider_models")

                if hasattr(self, "agent") and self.agent:
                    if hasattr(self.agent, "tool_cache"):
                        self.agent.tool_cache.clear()
                        cleared.append("tool_cache")
                    if hasattr(self.agent, "_stable_prompt_cache"):
                        self.agent._stable_prompt_cache.clear()
                        cleared.append("stable_prompt_cache")
                    if (
                        hasattr(self.agent, "vector_service")
                        and self.agent.vector_service
                    ):
                        if hasattr(self.agent.vector_service, "_emb_cache"):
                            self.agent.vector_service._emb_cache.clear()
                            cleared.append("embedding_cache")
                        if hasattr(self.agent.vector_service, "_grep_cache"):
                            self.agent.vector_service._grep_cache.clear()
                            cleared.append("grep_cache")

                if not cleared:
                    return {"status": "error", "message": "No cache sources available."}

                logger.info(f"Caches cleared via API: {', '.join(cleared)}")
                return {
                    "status": "success",
                    "message": f"Cleared: {', '.join(cleared)}",
                }
            except Exception as e:
                logger.error(f"Error clearing cache: {e}")
                return {"status": "error", "message": str(e)}

        @self.app.post(
            "/api/control/clear-logs", dependencies=[Depends(self.verify_auth)]
        )
        async def clear_logs():
            try:
                from pathlib import Path

                log_file = Path("logs/limebot.log")
                if log_file.exists():
                    await self._write_text(log_file, "")
                logger.info("Logs cleared via API.")
                return {"status": "success", "message": "Logs cleared."}
            except Exception as e:
                logger.error(f"Error clearing logs: {e}")
                return {"status": "error", "message": str(e)}

        @self.app.post(
            "/api/control/shutdown", dependencies=[Depends(self.verify_auth)]
        )
        async def shutdown_backend():
            import signal

            logger.warning("Shutdown requested via API...")

            async def _shutdown():
                await asyncio.sleep(0.5)
                if os.name == "nt":
                    os._exit(0)
                else:
                    os.kill(os.getpid(), signal.SIGINT)

            asyncio.create_task(_shutdown())
            return {"status": "shutting_down", "message": "Backend is shutting down..."}

        @self.app.post("/api/confirm-tool", dependencies=[Depends(self.verify_auth)])
        async def confirm_tool(data: dict):
            conf_id = data.get("conf_id")
            if not conf_id:
                raise HTTPException(status_code=400, detail="conf_id is required")
            if not hasattr(self, "agent"):
                raise HTTPException(status_code=500, detail="Agent not initialized")
            approved = data.get("approved", False)
            session_whitelist = data.get("session_whitelist", False)
            success = await self.agent.confirm_tool(
                conf_id, approved, session_whitelist, source="web"
            )
            if success:
                return {
                    "status": "success",
                    "message": f"Tool {conf_id} {'approved' if approved else 'denied'}",
                }
            raise HTTPException(
                status_code=404, detail="Confirmation request not found or expired"
            )

        @self.app.post(
            "/api/chat/{chat_id}/stop", dependencies=[Depends(self.verify_auth)]
        )
        async def stop_generation(chat_id: str):
            if not hasattr(self, "agent") or not self.agent:
                raise HTTPException(status_code=503, detail="Agent not initialized")

            session_key = _sanitize_web_session_key(chat_id)

            success = await self.agent.cancel_session(session_key)
            if success:
                return {"status": "success", "message": "Stopped generation"}
            return {"status": "ignored", "message": "No active task found to stop"}

        @self.app.post(
            "/api/chat/{chat_id}/messages/{message_id}/edit",
            dependencies=[Depends(self.verify_auth)],
        )
        async def edit_chat_message(chat_id: str, message_id: str, data: dict):
            if not hasattr(self, "agent") or not self.agent:
                raise HTTPException(status_code=503, detail="Agent not initialized")

            clean_message_id = str(message_id or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", clean_message_id):
                raise HTTPException(status_code=400, detail="message_id is invalid")

            new_content = str(data.get("content") or "").strip()
            if not new_content:
                raise HTTPException(status_code=400, detail="content is required")

            user_turn_index = data.get("user_turn_index")
            if not isinstance(user_turn_index, int) or user_turn_index < 0:
                raise HTTPException(
                    status_code=400, detail="user_turn_index must be a non-negative integer"
                )

            session_key = _sanitize_web_session_key(chat_id)
            active_task = getattr(self.agent, "active_tasks", {}).get(session_key)
            await self.agent.cancel_session(session_key)
            if active_task and not active_task.done():
                await asyncio.gather(active_task, return_exceptions=True)

            live_history = getattr(self.agent, "history", {}).get(session_key)
            truncated_history, history_found = await self.session_manager.truncate_history_from_user_turn(
                session_key,
                user_turn_index,
                history=live_history,
            )
            if not history_found:
                raise HTTPException(status_code=404, detail="Editable message not found")

            log_result = await self.session_manager.truncate_chat_log_from_user_message(
                session_key,
                clean_message_id,
                user_turn_index=user_turn_index,
            )

            if hasattr(self.agent, "history"):
                self.agent.history[session_key] = truncated_history
            if hasattr(self.agent, "_mark_dirty"):
                self.agent._mark_dirty(session_key)
            if hasattr(self.agent, "_flush_history"):
                await self.agent._flush_history(session_key, force=True)

            replay_message_id = f"usr_{uuid.uuid4().hex[:12]}"
            metadata = {
                "source": "web",
                "message_id": replay_message_id,
                "client_message_id": replay_message_id,
                "edited_from_message_id": clean_message_id,
            }
            client_metadata = data.get("metadata")
            if isinstance(client_metadata, dict):
                metadata.update(_extract_client_prompt_metadata({"metadata": client_metadata}))

            await self.session_manager.append_event_log(
                session_key,
                {
                    "type": "message_edited",
                    "chat_id": chat_id,
                    "replaced_message_id": clean_message_id,
                    "replacement_message_id": replay_message_id,
                    "user_turn_index": user_turn_index,
                    "matched_log_by_id": log_result.get("matched_by_id", False),
                    "removed_log_rows": log_result.get("removed", 0),
                    "content_preview": new_content[:500],
                },
            )

            await self._handle_message(
                sender_id="web-user",
                chat_id=chat_id,
                content=new_content,
                metadata=metadata,
            )

            return {
                "status": "queued",
                "chat_id": chat_id,
                "message_id": replay_message_id,
            }

        @self.app.post(
            "/api/whatsapp/send_file", dependencies=[Depends(self.verify_auth)]
        )
        async def send_whatsapp_file_api(to: str, file_path: str, caption: str = None):
            from channels.whatsapp import WhatsAppChannel

            wa_channel = next(
                (c for c in self.channels if isinstance(c, WhatsAppChannel)), None
            )
            if not wa_channel:
                return {"status": "error", "message": "WhatsApp channel not active"}

            try:
                success = await wa_channel.send_file(to, file_path, caption)
                return (
                    {"status": "success", "message": "File sent"}
                    if success
                    else {"status": "error", "message": "Failed to send file"}
                )
            except Exception as e:
                logger.error(f"API send_file error: {e}")
                return {"status": "error", "message": str(e)}

        @self.app.get(
            "/api/whatsapp/contacts", dependencies=[Depends(self.verify_auth)]
        )
        async def get_whatsapp_contacts():
            return await self._load_contacts_async()

        @self.app.post(
            "/api/whatsapp/contacts/approve", dependencies=[Depends(self.verify_auth)]
        )
        async def approve_whatsapp_contact(request: Request):
            data = await request.json()
            chat_id = data.get("chat_id")
            if not chat_id:
                return {"status": "error", "message": "Missing chat_id"}
            contacts = await self._load_contacts_async()
            if chat_id in contacts.get("pending", []):
                contacts["pending"].remove(chat_id)
            if chat_id in contacts.get("blocked", []):
                contacts["blocked"].remove(chat_id)
            if chat_id not in contacts.get("allowed", []):
                contacts.setdefault("allowed", []).append(chat_id)
            await self._save_contacts_async(contacts)
            return {"status": "success", "contacts": contacts}

        @self.app.post(
            "/api/whatsapp/contacts/deny", dependencies=[Depends(self.verify_auth)]
        )
        async def deny_whatsapp_contact(request: Request):
            data = await request.json()
            chat_id = data.get("chat_id")
            if not chat_id:
                return {"status": "error", "message": "Missing chat_id"}
            contacts = await self._load_contacts_async()
            if chat_id in contacts.get("pending", []):
                contacts["pending"].remove(chat_id)
            if chat_id in contacts.get("allowed", []):
                contacts["allowed"].remove(chat_id)
            if chat_id not in contacts.get("blocked", []):
                contacts.setdefault("blocked", []).append(chat_id)
            await self._save_contacts_async(contacts)
            return {"status": "success", "contacts": contacts}

        @self.app.post(
            "/api/whatsapp/contacts/unallow", dependencies=[Depends(self.verify_auth)]
        )
        async def unallow_whatsapp_contact(request: Request):
            data = await request.json()
            chat_id = data.get("chat_id")
            if not chat_id:
                return {"status": "error", "message": "Missing chat_id"}
            contacts = await self._load_contacts_async()
            if chat_id in contacts.get("allowed", []):
                contacts["allowed"].remove(chat_id)
            if chat_id not in contacts.get("pending", []):
                contacts.setdefault("pending", []).append(chat_id)
            await self._save_contacts_async(contacts)
            return {"status": "success", "contacts": contacts}

        @self.app.get("/api/whatsapp/status", dependencies=[Depends(self.verify_auth)])
        async def get_whatsapp_status():
            from channels.whatsapp import WhatsAppChannel

            wa_channel = next(
                (c for c in self.channels if isinstance(c, WhatsAppChannel)), None
            )
            if not wa_channel:
                return {
                    "status": "disabled",
                    "connected": False,
                    "bridge_connected": False,
                    "qr": None,
                }
            return {**wa_channel.get_status(), "qr": self._whatsapp_qr}

        @self.app.post("/api/whatsapp/reset", dependencies=[Depends(self.verify_auth)])
        async def reset_whatsapp_session():
            from channels.whatsapp import WhatsAppChannel

            wa_channel = next(
                (c for c in self.channels if isinstance(c, WhatsAppChannel)), None
            )
            if not wa_channel:
                return {"status": "error", "message": "WhatsApp channel not active"}
            try:
                success = await wa_channel.reset_session()
                if success:
                    self._whatsapp_qr = None
                    return {
                        "status": "success",
                        "message": "WhatsApp session reset initiated. Check UI for new QR code.",
                    }
                return {"status": "error", "message": "Failed to reset session"}
            except Exception as e:
                logger.error(f"Error resetting WhatsApp session: {e}")
                return {"status": "error", "message": str(e)}

        @self.app.get("/api/cron/jobs", dependencies=[Depends(self.verify_auth)])
        async def get_cron_jobs():
            if not self.scheduler:
                return []
            return await self.scheduler.list_jobs()

        @self.app.get("/api/telegram/status", dependencies=[Depends(self.verify_auth)])
        async def get_telegram_status():
            from config import load_config

            cfg = load_config()
            telegram_channel = next(
                (ch for ch in self.channels if getattr(ch, "name", "") == "telegram"),
                None,
            )
            if telegram_channel and hasattr(telegram_channel, "get_status"):
                return telegram_channel.get_status()
            return {
                "enabled": bool(getattr(cfg.telegram, "enabled", False)),
                "status": "disabled" if not getattr(cfg.telegram, "enabled", False) else "not_running",
                "connected": False,
                "username": None,
                "bot_id": None,
                "display_name": None,
                "can_join_groups": None,
                "can_read_all_group_messages": None,
                "supports_inline_queries": None,
                "last_error": "",
            }

        @self.app.post("/api/cron/jobs", dependencies=[Depends(self.verify_auth)])
        async def add_cron_job(data: dict):
            if not self.scheduler:
                raise HTTPException(status_code=503, detail="Scheduler not initialized")

            import re as _re

            time_expr = data.get("time_expr")
            cron_expr = data.get("cron_expr")
            message = data.get("message")

            if not message:
                raise HTTPException(status_code=400, detail="Missing message")
            if not time_expr and not cron_expr:
                raise HTTPException(
                    status_code=400, detail="Missing time_expr or cron_expr"
                )

            trigger_time = None
            if time_expr:
                multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
                suffix = time_expr[-1]
                if suffix not in multipliers:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid time format. Use '10s', '5m', '2h', '1d'.",
                    )
                match = _re.search(r"\d+", time_expr)
                if not match:
                    raise HTTPException(
                        status_code=400, detail="Could not parse time amount."
                    )
                trigger_time = time.time() + int(match.group()) * multipliers[suffix]

            context = data.get("context", {})
            if not context.get("channel"):
                context.update(
                    {"channel": "web", "chat_id": "manual_entry", "sender_id": "user"}
                )

            try:
                job_id = await self.scheduler.add_job(
                    trigger_time,
                    message,
                    context,
                    cron_expr=cron_expr,
                    tz_offset=data.get("tz_offset"),
                    tz=data.get("tz"),
                    name=data.get("name"),
                )
                return {
                    "status": "success",
                    "job_id": job_id,
                    "trigger_time": trigger_time,
                    "cron_expr": cron_expr,
                }
            except Exception as e:
                logger.error(f"Error adding job: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.delete(
            "/api/cron/jobs/{job_id}", dependencies=[Depends(self.verify_auth)]
        )
        async def delete_cron_job(job_id: str):
            if not self.scheduler:
                raise HTTPException(status_code=503, detail="Scheduler not initialized")
            success = await self.scheduler.remove_job(job_id)
            if success:
                return {"status": "success", "message": "Job deleted"}
            raise HTTPException(status_code=404, detail="Job not found")

        @self.app.patch(
            "/api/cron/jobs/{job_id}", dependencies=[Depends(self.verify_auth)]
        )
        async def update_cron_job(job_id: str, data: dict):
            if not self.scheduler:
                raise HTTPException(status_code=503, detail="Scheduler not initialized")

            if "active" not in data:
                raise HTTPException(status_code=400, detail="Missing active flag")

            updated = await self.scheduler.set_job_active(
                job_id, bool(data.get("active"))
            )
            if updated:
                return {"status": "success", "job": updated}
            raise HTTPException(status_code=404, detail="Job not found")

        # ── App-Server API Endpoints ────────────────────────────────────
        @self.app.get("/api/app/state", dependencies=[Depends(self.verify_app_auth)])
        async def get_app_state_api():
            return await self._get_app_state()

        @self.app.post("/api/app/workspaces", dependencies=[Depends(self.verify_app_auth)])
        async def create_app_workspace(data: dict):
            from core.task_tracker import get_task_tracker
            tracker = get_task_tracker()
            title = str(data.get("title") or "").strip()
            origin = str(data.get("origin") or "web").strip()
            if not title:
                raise HTTPException(status_code=400, detail="title is required")
            metadata = data.get("metadata")
            if isinstance(metadata, dict):
                metadata = _redact_sensitive_data(metadata)
            else:
                metadata = None
            requested_session_key = _canonicalize_app_workspace_session_key(
                data.get("session_key")
            )
            requested_chat_id = str(data.get("chat_id") or "").strip()
            workspace = await tracker.create_workspace(
                title=title,
                origin=origin,
                session_key=requested_session_key,
                chat_id=requested_chat_id,
                parent_workspace_id=str(data.get("parent_workspace_id") or "").strip(),
                metadata=metadata,
            )
            if not workspace.session_key:
                generated_session_key = _canonicalize_app_workspace_session_key(
                    f"app_{workspace.workspace_id}"
                )
                workspace = await tracker.update_workspace(
                    workspace.workspace_id,
                    session_key=generated_session_key,
                    chat_id=workspace.chat_id or generated_session_key[4:],
                    metadata_update={"app_session_key": generated_session_key},
                )
                if workspace is None:
                    raise HTTPException(status_code=404, detail="Workspace not found")
            return {"workspace": _serialize_workspace_for_app(workspace)}

        @self.app.get("/api/app/workspaces/{workspace_id}/events", dependencies=[Depends(self.verify_app_auth)])
        async def get_workspace_events(
            workspace_id: str, after_sequence: int = 0, limit: int = 500
        ):
            from core.task_tracker import get_task_tracker
            from core.session_manager import EVENTS_DIR
            tracker = get_task_tracker()
            workspace = await tracker.get_workspace(workspace_id)
            if not workspace:
                raise HTTPException(status_code=404, detail="Workspace not found")
            session_key = _canonicalize_app_workspace_session_key(
                workspace.session_key
            ) or _canonicalize_app_workspace_session_key(
                workspace.metadata.get("app_session_key")
            )
            if not session_key:
                return {"events": []}
            events_file = Path(EVENTS_DIR) / f"{session_key}.jsonl"
            if not events_file.exists():
                return {"events": []}
            events = []
            after_sequence = max(0, int(after_sequence or 0))
            limit = max(1, min(int(limit or 500), 1000))
            try:
                content_file = await self._read_text(events_file)
                replay_sequence = 0
                for line in content_file.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        event_type = evt.get("type")
                        timestamp = evt.get("timestamp") or time.time()
                        replay_sequence += 1
                        clean_evt = {}
                        for k, v in evt.items():
                            if k not in ("args", "result"):
                                if k == "preview" and isinstance(v, dict):
                                    clean_preview = {pk: pv for pk, pv in v.items() if pk != "command"}
                                    clean_evt[k] = _redact_sensitive_data(clean_preview)
                                elif k == "changeset" and isinstance(v, dict):
                                    from core.review_entrypoint import changeset_for_app

                                    clean_evt[k] = changeset_for_app(v)
                                else:
                                    clean_evt[k] = _redact_sensitive_data(v)
                        event = {
                            "type": "workspace_event",
                            "schema_version": 1,
                            "event_id": str(
                                evt.get("event_id")
                                or f"legacy_{workspace_id}_{replay_sequence}"
                            ),
                            "sequence": (
                                evt.get("sequence")
                                if isinstance(evt.get("sequence"), int)
                                else replay_sequence
                            ),
                            "workspace_id": workspace_id,
                            "session_key": session_key,
                            "task_id": evt.get("task_id"),
                            "turn_id": evt.get("turn_id"),
                            "event": event_type,
                            "payload": clean_evt,
                            "timestamp": timestamp,
                        }
                        if event["sequence"] <= after_sequence:
                            continue
                        outcome = _app_attempt_terminal_outcome(
                            clean_evt, str(event_type or "")
                        )
                        event["terminal"] = bool(outcome)
                        if outcome:
                            event["status"] = outcome[0]
                        events.append(event)
                        if len(events) >= limit:
                            break
                    except Exception:
                        continue
            except Exception as e:
                logger.error(f"Error reading workspace events: {e}")
            return {"events": events}

        @self.app.get(
            "/api/app/workspaces/{workspace_id}/changesets/{artifact_id}",
            dependencies=[Depends(self.verify_app_auth)],
        )
        async def get_app_changeset(workspace_id: str, artifact_id: str):
            from core.task_tracker import get_task_tracker

            workspace = await get_task_tracker().get_workspace(workspace_id)
            if workspace is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            artifact = next(
                (
                    item
                    for item in workspace.artifacts
                    if item.artifact_id == artifact_id and item.kind == "change_set"
                ),
                None,
            )
            if artifact is None:
                raise HTTPException(status_code=404, detail="Change set not found")
            return {"artifact": _serialize_artifact_for_app(artifact)}

        @self.app.post(
            "/api/app/workspaces/{workspace_id}/changesets",
            dependencies=[Depends(self.verify_app_auth)],
        )
        async def stage_app_changeset(workspace_id: str, data: dict):
            """Stage a redacted review artifact; this endpoint never applies files."""
            from core.review_entrypoint import build_changeset_artifact
            from core.task_tracker import get_task_tracker

            tracker = get_task_tracker()
            workspace = await tracker.get_workspace(workspace_id)
            if workspace is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            diff_text = data.get("diff")
            if not isinstance(diff_text, str) or not diff_text.strip():
                raise HTTPException(status_code=400, detail="diff is required")
            try:
                changeset = build_changeset_artifact(
                    diff_text,
                    status="awaiting_approval",
                    summary=str(data.get("summary") or "Patch review"),
                    verification=data.get("verification"),
                    preconditions=data.get("preconditions"),
                )
                changeset["id"] = f"changeset-{uuid.uuid4().hex[:12]}"
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            artifact = await tracker.add_workspace_artifact(
                workspace_id,
                kind="change_set",
                title="Patch review",
                metadata=changeset,
            )
            if artifact is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            return {"artifact": _serialize_artifact_for_app(artifact)}

        @self.app.post("/api/app/workspaces/{workspace_id}/message", dependencies=[Depends(self.verify_app_auth)])
        async def post_workspace_message(workspace_id: str, data: dict):
            from core.task_tracker import get_task_tracker, TaskStatus
            tracker = get_task_tracker()
            content_msg = str(data.get("content") or "").strip()
            if not content_msg:
                raise HTTPException(status_code=400, detail="content is required")
            workspace = await tracker.get_workspace(workspace_id)
            if workspace is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            app_session_key = _canonicalize_app_workspace_session_key(
                workspace.session_key
            ) or _canonicalize_app_workspace_session_key(
                workspace.metadata.get("app_session_key")
            ) or _canonicalize_app_workspace_session_key(f"app_{workspace_id}")
            session_id = app_session_key[4:]
            chat_id = workspace.chat_id or session_id
            client_message_id = str(data.get("client_message_id") or "").strip()
            if client_message_id and not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,120}", client_message_id
            ):
                raise HTTPException(
                    status_code=400, detail="client_message_id is invalid"
                )
            has_active = any(
                a.status in (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value)
                for a in workspace.attempts
            )
            initial_status = TaskStatus.QUEUED.value if has_active else TaskStatus.RUNNING.value
            await tracker.update_workspace(
                workspace_id,
                status=TaskStatus.RUNNING.value,
                session_key=app_session_key,
                chat_id=chat_id,
                metadata_update={"app_session_key": app_session_key},
            )
            attempt = await tracker.add_workspace_attempt(
                workspace_id,
                model=str(getattr(self.config.llm, "model", "")),
                summary=content_msg[:300],
                status=initial_status,
                metadata={"source": "app"},
            )
            if attempt is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            self._app_chat_workspaces[chat_id] = workspace_id
            self._app_chat_sessions[chat_id] = app_session_key
            self._app_workspace_attempts.setdefault(workspace_id, []).append(
                attempt.attempt_id
            )
            await self._handle_message(
                sender_id="app-user",
                chat_id=chat_id,
                content=content_msg,
                metadata={
                    "source": "app",
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "client_message_id": client_message_id,
                },
            )
            queued_event_id = f"evt_{uuid.uuid4().hex[:20]}"
            queued_event = {
                "type": "workspace_event",
                "schema_version": 1,
                "event_id": queued_event_id,
                "workspace_id": workspace_id,
                "session_key": app_session_key,
                "event": "message_queued",
                "payload": {
                    "attempt_id": attempt.attempt_id,
                    "client_message_id": client_message_id or None,
                },
                "timestamp": time.time(),
            }
            await self.session_manager.append_event_log(
                app_session_key,
                {
                    "type": "message_queued",
                    "event_id": queued_event_id,
                    "workspace_id": workspace_id,
                    "attempt_id": attempt.attempt_id,
                    "client_message_id": client_message_id or None,
                },
            )
            await self._broadcast_app_event(queued_event)
            return {
                "status": "queued",
                "workspace_id": workspace_id,
                "attempt_id": attempt.attempt_id,
                "session_key": app_session_key,
            }

        @self.app.post("/api/app/approvals/{conf_id}", dependencies=[Depends(self.verify_app_auth)])
        async def post_app_approval(conf_id: str, data: dict):
            if not conf_id:
                raise HTTPException(status_code=400, detail="conf_id is required")
            if "approved" not in data or not isinstance(data.get("approved"), bool):
                raise HTTPException(
                    status_code=400, detail="approved must be a boolean"
                )
            agent = getattr(self, "agent", None)
            if agent is None:
                raise HTTPException(status_code=503, detail="Agent not initialized")
            session_whitelist = data.get("session_whitelist", False)
            if not isinstance(session_whitelist, bool):
                raise HTTPException(
                    status_code=400, detail="session_whitelist must be a boolean"
                )
            client_source = str(data.get("source") or "app").strip().lower()
            if client_source not in {"app", "extension"}:
                client_source = "app"
            success = await agent.confirm_tool(
                conf_id,
                data["approved"],
                session_whitelist,
                source=client_source,
            )
            if not success:
                raise HTTPException(
                    status_code=404,
                    detail="Confirmation request not found or expired",
                )
            return {
                "status": "approved" if data["approved"] else "denied",
                "conf_id": conf_id,
            }

        @self.app.get("/api/ready")
        async def get_readiness(request: Request, x_api_key: str = Header(None)):
            if self._is_auth_required():
                internal_key = getattr(self.config.whitelist, "api_key", None)
                if x_api_key != internal_key:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid or missing API Key",
                    )
            agent = getattr(self, "agent", None)
            if not agent:
                return JSONResponse(
                    status_code=503,
                    content={"ready": False, "phase": "agent"}
                )
            status_data = agent.get_readiness_status()
            clean_status = _serialize_readiness_for_app(status_data)
            if not clean_status.get("ready", False):
                return JSONResponse(
                    status_code=503,
                    content=clean_status
                )
            return clean_status

        @self.app.get("/api/live")
        async def get_liveness():
            return {
                "status": "live",
                "version": "1.0.12",
                "boot_id": self._boot_id,
            }

        @self.app.post("/api/setup/complete")
        async def setup_complete(data: dict):
            env = data.get("env") or {}
            unknown_keys = set(env.keys()) - _ALLOWED_SETUP_ENV_KEYS
            if unknown_keys:
                return JSONResponse(
                    status_code=422,
                    content={"code": "unknown_fields"}
                )
            await self._persist_config_values(env, activate_config=False)
            try:
                old_env = {}
                for k, v in env.items():
                    if k in os.environ:
                        old_env[k] = os.environ[k]
                    os.environ[k] = str(v)
                from config import reload_config
                reload_config()
                try:
                    latency = await asyncio.wait_for(
                        self._probe_setup_llm(),
                        timeout=_SETUP_LLM_PROBE_TIMEOUT_SECONDS,
                    )
                finally:
                    for k in env:
                        if k in old_env:
                            os.environ[k] = old_env[k]
                        else:
                            os.environ.pop(k, None)
                    reload_config()
                restart_token = uuid.uuid4().hex
                try:
                    _SETUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _SETUP_STATE_PATH.write_text(
                        json.dumps({"restart_token": restart_token}),
                        encoding="utf-8"
                    )
                except Exception as e:
                    logger.error(f"Failed to write setup state: {e}")
                _schedule_restart()
                return {
                    "status": "restarting",
                    "latency_ms": latency,
                    "restart_token": restart_token,
                    "boot_id": self._boot_id,
                }
            except asyncio.TimeoutError:
                logger.warning("Setup LLM validation timed out.")
                return JSONResponse(
                    status_code=422,
                    content={
                        "stage": "llm_check",
                        "code": "provider_timeout",
                        "config_saved": True,
                        "retryable": True,
                    }
                )
            except Exception as e:
                return JSONResponse(
                    status_code=422,
                    content={
                        "stage": "llm_check",
                        "code": "invalid_credentials",
                        "config_saved": True,
                    }
                )

        @self.app.websocket("/ws/app")
        async def websocket_app(websocket: WebSocket):
            await self._app_websocket_handler(websocket)

        # ── Observability Endpoints ─────────────────────────────────────
        @self.app.get("/api/tasks", dependencies=[Depends(self.verify_auth)])
        async def get_tasks():
            from core.task_tracker import get_task_tracker
            tasks = await get_task_tracker().list_tasks()
            return {"tasks": [asdict(task) for task in tasks]}

        @self.app.get("/api/task-runs", dependencies=[Depends(self.verify_auth)])
        async def get_task_runs(status: Optional[str] = None, limit: int = 100):
            from core.task_runs import get_task_run_store

            statuses = [status] if status else None
            runs = get_task_run_store().list_runs(
                statuses=statuses, limit=max(1, min(int(limit or 100), 500))
            )
            return {"task_runs": [run.to_dict() for run in runs]}

        @self.app.get(
            "/api/task-runs/{run_id}", dependencies=[Depends(self.verify_auth)]
        )
        async def get_task_run(run_id: str):
            from core.task_runs import get_task_run_store

            run = get_task_run_store().get(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Task run not found")
            return {"task_run": run.to_dict()}

        @self.app.post(
            "/api/task-runs/{run_id}/cancel", dependencies=[Depends(self.verify_auth)]
        )
        async def cancel_task_run(run_id: str):
            from core.task_runs import get_task_run_store

            run = get_task_run_store().get(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Task run not found")
            if self.agent is not None and run.metadata.get("session_key"):
                await self.agent.cancel_session(str(run.metadata["session_key"]))
            else:
                run = get_task_run_store().cancel(run_id)
            return {"task_run": (run.to_dict() if run else {})}

        @self.app.get(
            "/api/tasks/{task_id}", dependencies=[Depends(self.verify_auth)]
        )
        async def get_task(task_id: str):
            from core.task_tracker import get_task_tracker

            task = await get_task_tracker().get_task(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            return {"task": asdict(task)}

        @self.app.post(
            "/api/tasks/{task_id}/wait", dependencies=[Depends(self.verify_auth)]
        )
        async def wait_task(task_id: str, timeout: Optional[float] = None):
            if not self.agent or not hasattr(self.agent, "wait_background_subagent_task"):
                raise HTTPException(status_code=503, detail="Agent is unavailable")
            try:
                task = await self.agent.wait_background_subagent_task(task_id, timeout)
            except asyncio.TimeoutError:
                from core.task_tracker import get_task_tracker

                task = await get_task_tracker().get_task(task_id)
                if task is None:
                    raise HTTPException(status_code=404, detail="Task not found")
                return {"task": asdict(task), "timed_out": True}
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            return {"task": asdict(task), "timed_out": False}

        @self.app.post(
            "/api/tasks/{task_id}/kill", dependencies=[Depends(self.verify_auth)]
        )
        async def kill_task(task_id: str):
            if not self.agent or not hasattr(self.agent, "kill_background_subagent_task"):
                raise HTTPException(status_code=503, detail="Agent is unavailable")
            task = await self.agent.kill_background_subagent_task(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            return {"task": asdict(task)}

        @self.app.get("/api/workspaces", dependencies=[Depends(self.verify_auth)])

        async def get_workspaces(
            status: Optional[str] = None,
            origin: Optional[str] = None,
            active_only: bool = False,
            limit: int = 100,
        ):

            from core.task_tracker import get_task_tracker

            workspaces = await get_task_tracker().list_workspaces(
                status_filter=status,
                origin_filter=origin,
                active_only=active_only,
                limit=limit,
            )

            return {"workspaces": [asdict(workspace) for workspace in workspaces]}

        @self.app.get(
            "/api/workspaces/{workspace_id}", dependencies=[Depends(self.verify_auth)]
        )

        async def get_workspace(workspace_id: str):

            from core.task_tracker import get_task_tracker

            workspace = await get_task_tracker().get_workspace(workspace_id)
            if workspace is None:
                raise HTTPException(status_code=404, detail="Workspace not found")

            return {"workspace": asdict(workspace)}

        @self.app.post("/api/workspaces", dependencies=[Depends(self.verify_auth)])

        async def create_workspace(data: dict):

            from core.task_tracker import get_task_tracker

            title = str(data.get("title") or "").strip()
            origin = str(data.get("origin") or "").strip() or "web"
            if not title:
                raise HTTPException(status_code=400, detail="title is required")

            workspace = await get_task_tracker().create_workspace(
                title,
                origin,
                session_key=str(data.get("session_key") or "").strip(),
                chat_id=str(data.get("chat_id") or "").strip(),
                parent_workspace_id=str(data.get("parent_workspace_id") or "").strip(),
                metadata=(
                    data.get("metadata")
                    if isinstance(data.get("metadata"), dict)
                    else None
                ),
            )

            return {"workspace": asdict(workspace)}

        @self.app.post(
            "/api/workspaces/{workspace_id}/status",
            dependencies=[Depends(self.verify_auth)],
        )

        async def update_workspace_status(workspace_id: str, data: dict):

            from core.task_tracker import get_task_tracker

            workspace = await get_task_tracker().update_workspace(
                workspace_id,
                status=str(data.get("status") or "").strip() or None,
                title=str(data.get("title") or "").strip() or None,
                error=str(data.get("error") or "").strip() or None,
                metadata_update=(
                    data.get("metadata")
                    if isinstance(data.get("metadata"), dict)
                    else None
                ),
            )
            if workspace is None:
                raise HTTPException(status_code=404, detail="Workspace not found")

            return {"workspace": asdict(workspace)}

        @self.app.post(
            "/api/workspaces/{workspace_id}/artifacts",
            dependencies=[Depends(self.verify_auth)],
        )

        async def add_workspace_artifact(workspace_id: str, data: dict):

            from core.task_tracker import get_task_tracker

            kind = str(data.get("kind") or "").strip()
            title = str(data.get("title") or "").strip()
            if not kind or not title:
                raise HTTPException(
                    status_code=400, detail="kind and title are required"
                )

            artifact = await get_task_tracker().add_workspace_artifact(
                workspace_id,
                kind=kind,
                title=title,
                path=str(data.get("path") or "").strip(),
                url=str(data.get("url") or "").strip(),
                metadata=(
                    data.get("metadata")
                    if isinstance(data.get("metadata"), dict)
                    else None
                ),
            )
            if artifact is None:
                raise HTTPException(status_code=404, detail="Workspace not found")

            return {"artifact": asdict(artifact)}

        @self.app.get("/api/deliveries", dependencies=[Depends(self.verify_auth)])
        async def get_deliveries():
            from core.delivery_tracker import get_delivery_tracker
            deliveries = await get_delivery_tracker().list_deliveries()
            return {"deliveries": deliveries}

        @self.app.get("/api/browser/sessions", dependencies=[Depends(self.verify_auth)])
        async def get_browser_sessions():
            from core.browser_sessions import get_browser_session_manager
            sessions = await get_browser_session_manager().list_sessions()
            return {"sessions": sessions}

        @self.app.delete("/api/browser/sessions/{session_id}", dependencies=[Depends(self.verify_auth)])
        async def delete_browser_session(session_id: str):
            from core.browser_sessions import get_browser_session_manager
            mgr = get_browser_session_manager()
            res = await mgr.delete_session(session_id)
            if res.get("success"):
                return {"status": "ok"}
            else:
                raise HTTPException(status_code=404, detail=res.get("error", "Session not found or could not be removed"))

        # ── ElevenLabs Voice Endpoints ─────────────────────────────────
        @self.app.get("/api/voice/settings", dependencies=[Depends(self.verify_auth)])
        async def get_voice_settings():
            from core.tts import ElevenLabsTTS
            return {
                "has_key": bool(ElevenLabsTTS.get_api_key()),
                "settings": ElevenLabsTTS.get_voice_config()
            }

        @self.app.post("/api/voice/settings", dependencies=[Depends(self.verify_auth)])
        async def save_voice_settings(data: dict):
            from core.tts import ElevenLabsTTS
            ElevenLabsTTS.save_voice_config(data)
            return {"status": "success", "settings": ElevenLabsTTS.get_voice_config()}

        @self.app.get("/api/voice/voices", dependencies=[Depends(self.verify_auth)])
        async def get_voice_list():
            from core.tts import ElevenLabsTTS
            voices = await ElevenLabsTTS.list_voices()
            return {"voices": voices}

        @self.app.post("/api/voice/synthesize", dependencies=[Depends(self.verify_auth)])
        async def synthesize_voice_preview(data: dict):
            from core.tts import ElevenLabsTTS
            text = data.get("text", "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="Text is required")
            
            # Temporary override settings for this preview if supplied
            voice_id = data.get("voice_id")
            settings = {
                "stability": data.get("stability"),
                "similarity_boost": data.get("similarity_boost"),
                "style": data.get("style"),
                "use_speaker_boost": data.get("use_speaker_boost"),
                "speed": data.get("speed"),
                "model_id": data.get("model_id"),
                "output_format": data.get("output_format")
            }
            # Clean up None values
            settings = {k: v for k, v in settings.items() if v is not None}
            
            # Since synthesize_and_save uses active config, let's temporarily do a custom synthesis
            try:
                audio_bytes = await ElevenLabsTTS.synthesize_text(text, voice_id=voice_id, settings=settings)
                import uuid
                from pathlib import Path
                
                temp_dir = Path("temp")
                temp_dir.mkdir(parents=True, exist_ok=True)
                
                filename = f"preview_{uuid.uuid4().hex[:8]}.mp3"
                filepath = temp_dir / filename
                
                with open(filepath, "wb") as f:
                    f.write(audio_bytes)
                
                return {"status": "success", "url": f"/temp/{filename}"}
            except Exception as e:
                logger.error(f"[TTS] Preview synthesis failed: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.websocket("/ws")
        async def websocket_root(websocket: WebSocket):
            await self._websocket_handler(websocket)

        @self.app.websocket("/ws/client")
        async def websocket_client(websocket: WebSocket):
            await self._websocket_handler(websocket)

    async def _websocket_handler(self, websocket: WebSocket) -> None:
        """Shared handler for all WebSocket connections."""
        await websocket.accept()
        if not await self._authenticate_websocket(websocket):
            return
        self.active_connections.add(websocket)
        logger.info(f"Web client connected ({websocket.url.path})")

        if self._whatsapp_qr:
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "whatsapp_qr",
                            "content": "WhatsApp QR Code",
                            "sender": "bot",
                            "chat_id": "system",
                            "metadata": {
                                "type": "whatsapp_qr",
                                "qr": self._whatsapp_qr,
                            },
                        }
                    )
                )
            except Exception as e:
                logger.error(f"Error sending cached QR: {e}")

        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("Received invalid JSON from web client")
                    continue

                if msg.get("type") == "confirmation_response":
                    conf_id = str(msg.get("confirmation_id") or "").strip()
                    approved = bool(msg.get("approved", False))
                    session_whitelist = bool(msg.get("session_whitelist", False))
                    success = False
                    if conf_id and hasattr(self, "agent"):
                        success = await self.agent.confirm_tool(
                            conf_id,
                            approved,
                            session_whitelist,
                            source="web",
                        )
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "confirmation_result",
                                "confirmation_id": conf_id,
                                "approved": approved,
                                "success": bool(success),
                            }
                        )
                    )
                    continue

                chat_id = msg.get("chat_id") or msg.get("sessionId") or "web-chat"
                content = msg.get("content", "")
                # Allow callers (e.g. specialized skills) to supply their
                # own sender_id so each user gets a separate LimeBot profile.
                sender_id = msg.get("sender_id") or "web-user"
                sender_name = msg.get("sender_name") or ""

                metadata = {"source": "web"}
                metadata.update(_extract_client_prompt_metadata(msg))
                client_message_id = str(msg.get("client_message_id") or "").strip()
                if client_message_id:
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", client_message_id):
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "message",
                                    "content": "Message id is invalid.",
                                    "sender": "bot",
                                    "chat_id": chat_id,
                                    "metadata": {"is_error": True},
                                }
                            )
                        )
                        continue
                    metadata["message_id"] = client_message_id
                    metadata["client_message_id"] = client_message_id
                if sender_name:
                    metadata["sender_name"] = sender_name
                attachment_paths: list[str] = []
                if raw_attachments := msg.get("attachments"):
                    try:
                        attachments, first_image_data_url = (
                            await self._normalize_web_attachments(
                                str(chat_id), raw_attachments
                            )
                        )
                    except ValueError as e:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "message",
                                    "content": str(e),
                                    "sender": "bot",
                                    "chat_id": chat_id,
                                    "metadata": {"is_error": True},
                                }
                            )
                        )
                        continue

                    if attachments:
                        metadata["attachments"] = attachments
                        attachment_paths = [
                            str(a.get("path"))
                            for a in attachments
                            if a.get("path")
                        ]
                    if first_image_data_url:
                        metadata["image"] = first_image_data_url
                elif image_data := msg.get("image"):
                    metadata["image"] = image_data

                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=content,
                    media=attachment_paths,
                    metadata=metadata,
                )

        except WebSocketDisconnect:
            pass
        finally:
            self.active_connections.discard(websocket)
            logger.info("Web client disconnected")

    async def _app_websocket_handler(self, websocket: WebSocket) -> None:
        await websocket.accept()
        internal_key = getattr(self.config.whitelist, "api_key", None)
        if not internal_key:
            await websocket.send_text(
                json.dumps({"type": "error", "code": "app_auth_required"})
            )
            await self._close_websocket_safely(websocket)
            return
        if not await self._authenticate_websocket(websocket):
            return
        self.app_connections.add(websocket)
        try:
            await websocket.send_text(
                json.dumps({"type": "app_state", "state": await self._get_app_state()})
            )
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(
                        json.dumps({"type": "error", "code": "invalid_json"})
                    )
                    continue
                if message.get("type") == "ping":
                    await websocket.send_text(
                        json.dumps({"type": "pong", "timestamp": time.time()})
                    )
                elif message.get("type") == "refresh_state":
                    await websocket.send_text(
                        json.dumps(
                            {"type": "app_state", "state": await self._get_app_state()}
                        )
                    )
                else:
                    await websocket.send_text(
                        json.dumps({"type": "error", "code": "unsupported_message"})
                    )
        except WebSocketDisconnect:
            pass
        finally:
            self.app_connections.discard(websocket)

    async def _get_app_state(self) -> dict:
        from core.task_tracker import get_task_tracker
        from core.task_runs import get_task_run_store
        tracker = get_task_tracker()
        workspaces = await tracker.list_workspaces()
        tasks = await tracker.list_tasks(limit=200)
        task_runs = get_task_run_store().list_runs(limit=200)
        pending_approvals = []
        agent = getattr(self, "agent", None)
        if agent and hasattr(agent, "pending_confirmations"):
            for conf_id, conf in agent.pending_confirmations.items():
                pending_approvals.append(_serialize_pending_approval_for_app(conf_id, conf))
        readiness = {}
        if agent and hasattr(agent, "get_readiness_status"):
            readiness = _serialize_readiness_for_app(agent.get_readiness_status())
        serialized_runs = []
        for run in task_runs:
            serialized_runs.append(
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "phase": run.phase,
                    "goal": _redact_sensitive_data(run.goal[:500]),
                    "acceptance_criteria": list(run.acceptance_criteria)[:8],
                    "current_step": run.current_step,
                    "workspace_id": run.workspace_id,
                    "provider": run.provider,
                    "slice_count": run.slice_count,
                    "corrective_failures": run.corrective_failures,
                    "max_slices": run.max_slices,
                    "max_corrective_failures": run.max_corrective_failures,
                    "last_error": _redact_sensitive_data(run.last_error[:500]),
                    "next_action": _redact_sensitive_data(run.next_action[:500]),
                    "updated_at": run.updated_at,
                }
            )
        return {
            "workspaces": [_serialize_workspace_for_app(w) for w in workspaces],
            "tasks": [_serialize_task_for_app(task) for task in tasks],
            "task_runs": serialized_runs,
            "pending_approvals": pending_approvals,
            "runtime": {
                "readiness": readiness,
            }
        }

    def _decorate_app_event(self, event: dict) -> dict:
        """Add stable identity and terminal metadata to every app event."""
        normalized = dict(event or {})
        workspace_id = str(normalized.get("workspace_id") or "")
        session_key = str(normalized.get("session_key") or "")
        sequence_key = workspace_id or session_key or "global"
        supplied_sequence = normalized.get("sequence")
        if isinstance(supplied_sequence, int) and supplied_sequence > 0:
            sequence = supplied_sequence
            self._app_event_sequences[sequence_key] = max(
                self._app_event_sequences.get(sequence_key, 0), sequence
            )
        else:
            sequence = self._app_event_sequences.get(sequence_key, 0) + 1
            self._app_event_sequences[sequence_key] = sequence
        normalized.setdefault("schema_version", 1)
        if not normalized.get("event_id"):
            normalized["event_id"] = f"evt_{uuid.uuid4().hex[:20]}"
        normalized["sequence"] = sequence
        payload = normalized.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        outcome = _app_attempt_terminal_outcome(
            payload, str(normalized.get("event") or "")
        )
        normalized["terminal"] = bool(outcome)
        if outcome:
            normalized["status"] = outcome[0]
        normalized.setdefault("timestamp", time.time())
        return normalized

    async def _broadcast_app_event(self, event: dict) -> None:
        if not self.app_connections:
            return
        event = self._decorate_app_event(event)
        payload = json.dumps(event)
        dead = set()
        for conn in list(self.app_connections):
            try:
                await asyncio.wait_for(conn.send_text(payload), timeout=2.0)
            except Exception:
                dead.add(conn)
        for conn in dead:
            self.app_connections.discard(conn)

    async def _emit_app_outbound(self, msg: OutboundMessage, metadata: dict, msg_type: str) -> None:
        chat_id_str = str(msg.chat_id)
        workspace_id = self._app_chat_workspaces.get(chat_id_str)
        session_key = self._app_chat_sessions.get(chat_id_str)
        if not workspace_id or not session_key:
            return
        clean_metadata = {}
        for k, v in metadata.items():
            if k not in ("args", "result"):
                if k == "preview" and isinstance(v, dict):
                    clean_preview = {pk: pv for pk, pv in v.items() if pk != "command"}
                    clean_metadata[k] = _redact_sensitive_data(clean_preview)
                elif k == "changeset" and isinstance(v, dict):
                    from core.review_entrypoint import changeset_for_app

                    clean_metadata[k] = changeset_for_app(v)
                else:
                    clean_metadata[k] = _redact_sensitive_data(v)
        payload = {
            "type": "workspace_event",
            "schema_version": 1,
            "event_id": metadata.get("event_id"),
            "workspace_id": workspace_id,
            "session_key": session_key,
            "task_id": metadata.get("task_id"),
            "turn_id": metadata.get("turn_id"),
            "event": msg_type,
            "payload": clean_metadata,
            "timestamp": time.time(),
        }
        await self._broadcast_app_event(payload)

    async def _persist_config_values(self, env: dict, activate_config: bool = True) -> Any:
        from pathlib import Path
        env_file = Path(".env")
        new_env = dict(env)
        if "ALLOWED_PATHS" in new_env:
            paths_data = new_env.pop("ALLOWED_PATHS")
            paths_file = Path("allowed_paths.txt")
            if isinstance(paths_data, list):
                paths = [str(p).strip() for p in paths_data if str(p).strip()]
            else:
                paths = [
                    p.strip() for p in str(paths_data).split(",") if p.strip()
                ]
            tmp_paths = paths_file.with_suffix(f".{uuid.uuid4().hex}.tmp")
            await self._write_text(tmp_paths, "\n".join(paths))
            await asyncio.to_thread(tmp_paths.replace, paths_file)
        current_lines = []
        if env_file.exists():
            current_lines = (await self._read_text(env_file)).splitlines()
        updates = {str(k): str(v) for k, v in new_env.items()}
        merged_lines = _merge_env_lines(current_lines, updates, set())
        tmp_env = env_file.with_suffix(f".{uuid.uuid4().hex}.tmp")
        await self._write_text(tmp_env, "\n".join(merged_lines) + "\n")
        await asyncio.to_thread(tmp_env.replace, env_file)
        if activate_config:
            for key, val in updates.items():
                os.environ[key] = str(val)
            from config import reload_config
            reload_config()
        return self.config

    async def _probe_setup_llm(self) -> int:
        from config import load_config
        cfg = load_config()
        model = cfg.llm.model
        start = time.time()
        provider = self.llm_client.resolve_provider(
            model, default_base_url=cfg.llm.base_url
        )
        await self.llm_client.complete(
            provider,
            ChatRequest(
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
                session_id="llm-setup-probe",
            )
        )
        latency = int((time.time() - start) * 1000)
        return latency

    async def start(self) -> None:

        # Safely get port from config, defaulting to 8000
        web_config = getattr(self.config, "web", None)
        base_port = getattr(web_config, "port", 8000) if web_config else 8000

        # Resolve the bind host. Default to loopback; only allow a non-loopback
        # bind when API authentication is configured. "No API key" implies
        # "localhost only" so we never expose an unauthenticated agent to the LAN.
        host = str(
            getattr(web_config, "host", None)
            or os.getenv("WEB_HOST", "127.0.0.1")
            or "127.0.0.1"
        ).strip()
        has_api_key = bool(
            getattr(getattr(self.config, "whitelist", None), "api_key", None)
            or os.getenv("APP_API_KEY")
        )
        trusted_proxy_only = str(
            os.getenv("LIMEBOT_TRUSTED_PROXY_ONLY", "false")
        ).strip().lower() in {"1", "true", "yes", "on"}
        resolved_host = resolve_web_bind_host(
            host,
            has_api_key=has_api_key,
            trusted_proxy_only=trusted_proxy_only,
        )
        if resolved_host != host:
            logger.critical(
                f"WEB_HOST is set to '{host}' but APP_API_KEY is not configured. "
                "Refusing to expose an unauthenticated agent off-box; forcing "
                "bind to 127.0.0.1. Set APP_API_KEY to enable LAN access."
            )
        elif host not in _LOOPBACK_WEB_HOSTS and trusted_proxy_only and not has_api_key:
            logger.warning(
                "Allowing unauthenticated container-network binding because "
                "LIMEBOT_TRUSTED_PROXY_ONLY=true. The backend port must not be "
                "published directly."
            )
        host = resolved_host
        self.bind_host = host
        prefer_base_port_only = os.environ.pop("LIMEBOT_SOFT_RESTART", "") == "1"
        base_port_wait_seconds = 12.0 if prefer_base_port_only else 5.0
        base_port_retry_interval = 0.5

        max_retries = 10
        last_base_port_error: OSError | None = None

        def _format_bind_error(port: int, exc: OSError) -> str:
            reason = getattr(exc, "strerror", None) or str(exc)
            code = getattr(exc, "errno", None)
            if code:
                return f"port {port} bind failed with errno {code}: {reason}"
            return f"port {port} bind failed: {reason}"

        async def _wait_for_preferred_port(port: int) -> bool:
            nonlocal last_base_port_error
            deadline = asyncio.get_running_loop().time() + base_port_wait_seconds
            while True:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.bind((host, port))
                    return True
                except OSError as e:
                    last_base_port_error = e
                    reason = _format_bind_error(port, e)
                    if asyncio.get_running_loop().time() >= deadline:
                        return False
                    logger.warning(
                        f"Port {port} is unavailable ({reason}); waiting before fallback..."
                    )
                    await asyncio.sleep(base_port_retry_interval)

        base_port_available = await _wait_for_preferred_port(base_port)
        if not base_port_available:
            if prefer_base_port_only:
                logger.warning(
                    f"Configured web port {base_port} did not become available after "
                    f"{base_port_wait_seconds:.1f}s during soft restart; "
                    f"{_format_bind_error(base_port, last_base_port_error) if last_base_port_error else 'cause unknown'}; "
                    "trying fallback ports."
                )
            else:
                logger.warning(
                    f"Configured web port {base_port} did not become available after "
                    f"{base_port_wait_seconds:.1f}s; "
                    f"{_format_bind_error(base_port, last_base_port_error) if last_base_port_error else 'cause unknown'}; "
                    "trying fallback ports."
                )

        start_offset = 0 if base_port_available else 1
        for port_offset in range(start_offset, max_retries):
            current_port = base_port + port_offset
            try:
                # First try to see if we can bind a socket to this port
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind((host, current_port))

                # If we got here, the port is likely available
                config = uvicorn.Config(
                    self.app, host=host, port=current_port, log_level="info"
                )
                self.server = uvicorn.Server(config)
                self.actual_port = current_port
                # Write the actual port so skills can discover it
                try:
                    from pathlib import Path

                    port_file = Path("data/.backend_port")
                    port_file.parent.mkdir(parents=True, exist_ok=True)
                    port_file.write_text(str(current_port), encoding="utf-8")
                except Exception:
                    pass
                logger.info(f"Web channel starting on {host}:{current_port}")

                try:
                    await self.server.serve()
                    return
                except (OSError, SystemExit) as e:
                    if isinstance(e, SystemExit) and e.code != 0:
                        logger.warning(
                            f"Uvicorn failed to start on port {current_port}, likely bind conflict."
                        )
                    else:
                        logger.warning(f"OS error on port {current_port}: {e}")

                    if port_offset < max_retries - 1:
                        logger.warning("Retrying next port...")
                        continue
                    else:
                        raise

            except OSError as e:
                if port_offset < max_retries - 1:
                    logger.warning(
                        f"Port {current_port} is unavailable ({_format_bind_error(current_port, e)}), "
                        f"trying {current_port + 1}..."
                    )
                    continue
                else:
                    logger.error(
                        f"Failed to find an available port after {max_retries} attempts; "
                        f"last error: {_format_bind_error(current_port, e)}"
                    )
                    raise
            except Exception as e:
                logger.exception(f"CRITICAL: WebChannel failed to start: {e}")
                break

    async def stop(self) -> None:
        if self.server:
            self.server.should_exit = True

    @staticmethod
    def _delivery_timeout(msg_type: str) -> float:
        """Ephemeral updates expire quickly; durable outcomes get a wider window."""
        return 0.25 if msg_type in EPHEMERAL_OUTBOUND_TYPES else 2.0

    async def _broadcast_chat_payload(self, payload: str, msg_type: str) -> None:
        dead: set[WebSocket] = set()
        timeout = self._delivery_timeout(msg_type)

        async def _safe_send(conn: WebSocket) -> None:
            try:
                await asyncio.wait_for(conn.send_text(payload), timeout=timeout)
            except Exception:
                dead.add(conn)

        if self.active_connections:
            await asyncio.gather(*(_safe_send(conn) for conn in list(self.active_connections)))
        # A connection that times out once is stale; do not make later chunks wait again.
        self.active_connections.difference_update(dead)

    async def send(self, msg: OutboundMessage) -> None:
        if (
            not self.active_connections
            and not self.app_connections
            and str(msg.chat_id) not in self._app_chat_workspaces
        ):
            return

        metadata = msg.metadata or {}
        msg_type = str(metadata.get("type", "message"))
        payload = json.dumps(
            {
                "type": msg_type,
                "content": msg.content,
                "sender": "bot",
                "chat_id": msg.chat_id,
                "turn_id": metadata.get("turn_id"),
                "message_id": metadata.get("message_id"),
                "metadata": metadata,
            }
        )
        # Browser delivery is the latency-critical path. App state bookkeeping follows it.
        await self._broadcast_chat_payload(payload, msg_type)
        await self._emit_app_outbound(msg, metadata, str(msg_type))

        chat_id_str = str(msg.chat_id)
        terminal_outcome = _app_attempt_terminal_outcome(metadata, str(msg_type))
        if chat_id_str in self._app_chat_workspaces and terminal_outcome:
            workspace_id = self._app_chat_workspaces[chat_id_str]
            from core.task_tracker import get_task_tracker, TaskStatus
            tracker = get_task_tracker()
            workspace = await tracker.get_workspace(workspace_id)
            if workspace:
                active_attempts = [
                    a for a in workspace.attempts
                    if a.status in (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value)
                ]
                if active_attempts:
                    oldest_attempt = active_attempts[0]
                    terminal_status, terminal_error = terminal_outcome
                    await tracker.complete_workspace_attempt(
                        workspace_id,
                        oldest_attempt.attempt_id,
                        status=terminal_status,
                        error=terminal_error,
                    )
                    if len(active_attempts) > 1:
                        next_attempt = active_attempts[1]
                        async with tracker._lock:
                            w_ref = tracker._workspaces.get(workspace_id)
                            if w_ref:
                                a_ref = next(
                                    (item for item in w_ref.attempts if item.attempt_id == next_attempt.attempt_id),
                                    None
                                )
                                if a_ref:
                                    a_ref.status = TaskStatus.RUNNING.value
                                    a_ref.started_at = time.time()
                                    a_ref.updated_at = time.time()
                        await tracker.update_workspace(
                            workspace_id,
                            status=TaskStatus.RUNNING.value
                        )
                    else:
                        await tracker.update_workspace(
                            workspace_id,
                            status=terminal_status,
                            error=terminal_error,
                        )

        if msg_type == "whatsapp_qr":
            self._whatsapp_qr = metadata.get("qr")
            logger.info(
                f"Cached WhatsApp QR (len={len(self._whatsapp_qr) if self._whatsapp_qr else 0})"
            )
        elif msg_type == "whatsapp_status":
            status = metadata.get("status")
            if status in {"connected", "disconnected"} and self._whatsapp_qr is not None:
                self._whatsapp_qr = None
                logger.info(f"Cleared WhatsApp QR cache ({status})")

def _spawn_restart() -> None:
    """Restart the current process after the response is sent."""
    os.environ["LIMEBOT_SOFT_RESTART"] = "1"
    os.execl(os.sys.executable, os.sys.executable, *os.sys.argv)
