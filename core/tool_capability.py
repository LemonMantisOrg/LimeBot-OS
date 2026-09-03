"""Host-side tool capability gate.

Prompt text and shortlist hints are not enough: the model still picks a nearby
wrong tool and the host used to execute it. This module refuses illegal
tool/arg combos BEFORE any handler runs, and shrinks the offered tool surface
when the current turn's attachments are images.

This is not a rewrite of browser, shell, or filesystem implementations.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence
from urllib.parse import unquote, urlparse

from core.media_intent import is_instagram_photo_send, is_write_and_run_request


IMAGE_FILE_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jpe",
        ".jfif",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".avif",
        ".ico",
    }
)
IMAGE_CONTENT_TYPES: FrozenSet[str] = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/pjpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/x-ms-bmp",
        "image/tiff",
        "image/heic",
        "image/heif",
        "image/avif",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)

# Tools that are the nearby-wrong way to "see" chat-attached images.
IMAGE_INSPECTION_BLOCKED_TOOLS: FrozenSet[str] = frozenset(
    {
        "read_file",
        "browser_navigate",
    }
)

# Nearby-wrong way to "see" an Instagram carousel on a download/send turn.
INSTAGRAM_PHOTO_BLOCKED_TOOLS: FrozenSet[str] = frozenset(
    {
        "browser_navigate",
        "browser_act",
        "browser_extract",
        "run_command",
        "run_steps",
        "web_search",
    }
)

_INSTAGRAM_POST_CMD_RE = re.compile(
    r"instagram\.com/(?:p|reel|reels)/",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_CHAT_UPLOAD_MARKERS = (
    "temp/discord_uploads/",
    "temp/web_uploads/",
    "temp\\discord_uploads\\",
    "temp\\web_uploads\\",
)
_TEXT_FILE_HINT_RE = re.compile(
    r"(?:"
    r"\.(?:py|ts|tsx|js|jsx|mjs|cjs|md|txt|json|toml|ya?ml|csv|html|css|rs|go|"
    r"java|kt|c|cc|cpp|h|hpp|rb|php|sh|bash|zsh|ini|cfg|conf|log|xml|sql|pdf|"
    r"docx?|xlsx?|pptx?)\b"
    r"|[/\\](?:readme|agents|license|makefile|dockerfile)\b"
    r"|\b(?:readme|agents\.md|license)\b"
    r")",
    re.IGNORECASE,
)
_FILESYSTEM_INTENT_RE = re.compile(
    r"\b(?:read|open|inspect|cat|show|edit|write|list|ls|dir|search)\b"
    r".{0,80}\b(?:file|files|directory|folder|code|repo|project|path)\b",
    re.IGNORECASE,
)

_JPEG_MAGIC = (b"\xff\xd8\xff",)
_PNG_MAGIC = (b"\x89PNG\r\n\x1a\n",)
_GIF_MAGIC = (b"GIF87a", b"GIF89a")
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"
_BMP_MAGIC = (b"BM",)


def _normalize_content_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw.split(";", 1)[0].strip()


def _locator_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw or raw.lower().startswith("data:"):
        parsed = urlparse(raw)
        return unquote(parsed.path or "")
    return raw.split("?", 1)[0].split("#", 1)[0]


def locator_suffix(value: str) -> str:
    """Return the file suffix of a path or URL, ignoring query strings."""
    path = _locator_path(value)
    if not path:
        return ""
    return Path(path).suffix.lower()


def is_image_content_type(value: Any) -> bool:
    normalized = _normalize_content_type(value)
    if not normalized:
        return False
    if normalized in IMAGE_CONTENT_TYPES:
        return True
    return normalized.startswith("image/") and normalized not in {
        "image/svg+xml",
    }


def is_image_locator(value: str, content_type: str = "") -> bool:
    """True when a path or URL names an image by suffix or content-type."""
    if is_image_content_type(content_type):
        return True
    raw = str(value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered.startswith("data:image/") and not lowered.startswith("data:image/svg"):
        return True
    suffix = locator_suffix(raw)
    return suffix in IMAGE_FILE_EXTENSIONS


def iter_urls(text: str) -> List[str]:
    return [match.group(0).rstrip(".,;:!?)") for match in _URL_RE.finditer(text or "")]


def strip_image_urls(text: str) -> str:
    """Remove image URLs so page/file detectors see the rest of the turn."""

    def _replace(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".,;:!?)")
        return "" if is_image_locator(url) else match.group(0)

    return _URL_RE.sub(_replace, text or "")


def user_named_a_real_page(text: str) -> bool:
    """True when the user named an HTML page, not an image URL."""
    # Imported lazily to avoid a tool_defs ↔ tool_capability cycle.
    from core.tool_defs import user_named_a_page

    return user_named_a_page(strip_image_urls(text or ""))


def user_requested_text_file(text: str) -> bool:
    """True when the user asked to inspect a real text/code/document file."""
    blob = strip_image_urls(text or "")
    if is_write_and_run_request(blob):
        return True
    if _TEXT_FILE_HINT_RE.search(blob):
        return True
    return bool(_FILESYSTEM_INTENT_RE.search(blob))


def turn_image_attachments(attachments: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    images: List[Dict[str, Any]] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        kind = str(attachment.get("kind") or "").strip().lower()
        mime = attachment.get("mime_type") or attachment.get("mimeType") or ""
        name = str(attachment.get("name") or attachment.get("filename") or "")
        path = str(attachment.get("path") or "")
        url = str(attachment.get("url") or "")
        if (
            kind == "image"
            or is_image_content_type(mime)
            or is_image_locator(name, mime)
            or is_image_locator(path, mime)
            or is_image_locator(url, mime)
        ):
            images.append(attachment)
    return images


def turn_has_image_attachments(attachments: Optional[Iterable[Any]]) -> bool:
    return bool(turn_image_attachments(attachments))


def _is_chat_upload_image_path(path: str, attachments: Optional[Iterable[Any]] = None) -> bool:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return False
    lowered = raw.lower()
    if any(marker.replace("\\", "/") in lowered for marker in _CHAT_UPLOAD_MARKERS):
        if is_image_locator(raw):
            return True
        for attachment in turn_image_attachments(attachments):
            attached = str(attachment.get("path") or "").replace("\\", "/")
            if attached and (attached in raw or raw.endswith(attached)):
                return True
    for attachment in turn_image_attachments(attachments):
        attached = str(attachment.get("path") or "").replace("\\", "/")
        if attached and (raw == attached or raw.endswith(attached)):
            return True
    return False


def _sniff_image_magic(path: str) -> bool:
    candidate = Path(str(path or "").strip())
    if not candidate.is_file():
        return False
    try:
        header = candidate.read_bytes()[:16]
    except OSError:
        return False
    if not header:
        return False
    if any(header.startswith(magic) for magic in _JPEG_MAGIC):
        return True
    if any(header.startswith(magic) for magic in _PNG_MAGIC):
        return True
    if any(header.startswith(magic) for magic in _GIF_MAGIC):
        return True
    if any(header.startswith(magic) for magic in _BMP_MAGIC):
        return True
    if header.startswith(_WEBP_RIFF) and _WEBP_TAG in header:
        return True
    return False


def is_image_read_target(
    path: str,
    *,
    content_type: str = "",
    attachments: Optional[Iterable[Any]] = None,
) -> bool:
    raw = str(path or "").strip()
    if not raw:
        return False
    if is_image_locator(raw, content_type):
        return True
    if _is_chat_upload_image_path(raw, attachments):
        return True
    return _sniff_image_magic(raw)


def is_image_navigate_target(
    url: str,
    *,
    content_type: str = "",
    attachments: Optional[Iterable[Any]] = None,
) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    if is_image_locator(raw, content_type):
        return True
    for attachment in turn_image_attachments(attachments):
        attached_url = str(attachment.get("url") or "").strip()
        attached_type = str(
            attachment.get("mime_type") or attachment.get("mimeType") or ""
        )
        if attached_url and raw.split("?", 1)[0] == attached_url.split("?", 1)[0]:
            return True
        if attached_url == raw:
            return True
        if is_image_content_type(attached_type) and attached_url and attached_url in raw:
            return True
    return False


def refuse_read_file_image(
    path: str,
    *,
    content_type: str = "",
    attachments: Optional[Iterable[Any]] = None,
) -> Optional[str]:
    if not is_image_read_target(
        path, content_type=content_type, attachments=attachments
    ):
        return None
    return (
        "Error: read_file cannot dump image bytes (jpeg/png/gif/webp and similar). "
        "Chat-attached photos are already in vision context — inspect them directly. "
        "Do not call browser_navigate on the image URL. "
        "Use web_search(kind=\"images\") only if the user asked you to find a "
        "different public photo."
    )


def refuse_browser_navigate_image(
    url: str,
    *,
    content_type: str = "",
    attachments: Optional[Iterable[Any]] = None,
) -> Optional[str]:
    if not is_image_navigate_target(
        url, content_type=content_type, attachments=attachments
    ):
        return None
    return (
        "Error: browser_navigate cannot open image URLs. "
        "This target is an image (path or content-type), not a webpage. "
        "Chat-attached photos are already in vision context. "
        "Use web_search(kind=\"images\") to find a public photo, or "
        "browser_navigate only for an HTML page."
    )


def refuse_instagram_photo_tools(
    function_name: str,
    function_args: Optional[Dict[str, Any]] = None,
    *,
    user_text: str = "",
) -> Optional[str]:
    """Refuse browser/curl as the way to download an Instagram carousel."""
    if not is_instagram_photo_send(user_text):
        return None
    name = str(function_name or "").strip()
    args = dict(function_args or {})
    from core.instagram import is_instagram_post_url

    if name in {"browser_navigate", "browser_act", "browser_extract"}:
        url = str(args.get("url") or args.get("path") or args.get("href") or "").strip()
        if name != "browser_navigate" or is_instagram_post_url(url) or not url:
            return (
                "Error: browser_navigate cannot open Instagram posts to download "
                "photos. The host fetches /p/{shortcode}/embed/captioned/ sidecar "
                "stills. Call send_media. A browser launch failure does not block "
                "send_media."
            )
    if name in {"run_command", "run_steps"}:
        command = str(args.get("command") or "")
        commands = args.get("commands") or []
        blob = command + " " + " ".join(str(item) for item in commands)
        if _INSTAGRAM_POST_CMD_RE.search(blob):
            return (
                "Error: Do not curl Instagram post HTML. og:image is one video "
                "thumbnail, not the carousel. The host fetches the public embed "
                "sidecar and send_media delivers the stills."
            )
    return None


def refuse_illegal_tool_call(
    function_name: str,
    function_args: Optional[Dict[str, Any]] = None,
    *,
    attachments: Optional[Iterable[Any]] = None,
    user_text: str = "",
) -> Optional[str]:
    """Return a recoverable Error string, or None if the host may execute."""
    name = str(function_name or "").strip()
    args = dict(function_args or {})
    instagram_refusal = refuse_instagram_photo_tools(
        name, args, user_text=user_text
    )
    if instagram_refusal:
        return instagram_refusal
    if name == "read_file":
        path = str(
            args.get("path") or args.get("file") or args.get("filename") or ""
        ).strip()
        content_type = str(args.get("content_type") or args.get("mime_type") or "")
        return refuse_read_file_image(
            path, content_type=content_type, attachments=attachments
        )
    if name == "browser_navigate":
        url = str(args.get("url") or args.get("path") or args.get("href") or "").strip()
        content_type = str(args.get("content_type") or args.get("mime_type") or "")
        return refuse_browser_navigate_image(
            url, content_type=content_type, attachments=attachments
        )
    return None


def hidden_tools_for_image_attachments(
    text: str = "",
    attachments: Optional[Iterable[Any]] = None,
) -> FrozenSet[str]:
    """Tools to hide when the turn's attachments are images.

    Real page-navigate and text-file-read requests keep those tools.
    """
    if not turn_has_image_attachments(attachments):
        return frozenset()
    hidden = set(IMAGE_INSPECTION_BLOCKED_TOOLS)
    if user_named_a_real_page(text):
        hidden.discard("browser_navigate")
    if user_requested_text_file(text):
        hidden.discard("read_file")
    return frozenset(hidden)


def hidden_tools_for_instagram_photo_send(text: str = "") -> FrozenSet[str]:
    """Hide browser/curl on an Instagram photo-download/send turn."""
    if not is_instagram_photo_send(text):
        return frozenset()
    return INSTAGRAM_PHOTO_BLOCKED_TOOLS


def filter_tools_for_image_attachments(
    tool_defs: Sequence[Dict[str, Any]],
    text: str = "",
    attachments: Optional[Iterable[Any]] = None,
) -> List[Dict[str, Any]]:
    hidden = hidden_tools_for_image_attachments(text, attachments)
    if not hidden:
        return list(tool_defs)
    filtered = [
        tool
        for tool in tool_defs
        if str(tool.get("function", {}).get("name") or "") not in hidden
    ]
    return filtered or list(tool_defs)


def filter_tools_for_instagram_photo_send(
    tool_defs: Sequence[Dict[str, Any]],
    text: str = "",
) -> List[Dict[str, Any]]:
    hidden = hidden_tools_for_instagram_photo_send(text)
    if not hidden:
        return list(tool_defs)
    return [
        tool
        for tool in tool_defs
        if str(tool.get("function", {}).get("name") or "") not in hidden
    ]
