"""Intent helpers for chat media delivery vs image generation.

A request like "download a picture of X and send it in this chat" must route
to host-owned ``web_search(kind="images")``. The host attaches the photo.
``generate_image`` is only for newly created art.
"""

from __future__ import annotations

import re
from typing import FrozenSet, Optional, Tuple

_DELIVERY_VERBS = (
    r"send|share|download|get|find|fetch|bring|attach|look\s*up|look\s*for|"
    r"busca(?:r)?|busque|env[ií]a(?:me|nos)?|"
    r"m[aá]nd[ae](?:me|nos)?|tr[aá]e(?:me|nos)?|p[aá]sa(?:me|nos)?|"
    r"descarga(?:r)?|adjunt(?:a|ar)|muestra(?:me)?|show"
)
_IMAGE_NOUNS = (
    r"pic|pics|photo|photos|picture|pictures|image|images|"
    r"imagen(?:es)?|im[aá]genes|foto|fotos|wallpaper"
)
_CREATE_VERBS = (
    r"generate|crear|crea|draw|render|paint|imagine|dall-?e|"
    r"make\s+(?:me\s+)?(?:an?\s+)?(?:new\s+)?|"
    r"create\s+(?:me\s+)?(?:an?\s+)?(?:new\s+)?"
)

_DELIVERY_RE = re.compile(
    rf"(?:\b(?:{_DELIVERY_VERBS})\b.{{0,100}}\b(?:{_IMAGE_NOUNS})\b|"
    rf"\b(?:{_IMAGE_NOUNS})\b.{{0,100}}\b(?:{_DELIVERY_VERBS})\b)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_RE = re.compile(
    rf"\b(?:{_CREATE_VERBS})\b.{{0,60}}\b(?:{_IMAGE_NOUNS}|art|illustration)\b",
    re.IGNORECASE | re.DOTALL,
)

_WRITE_RUN_RE = re.compile(
    r"(?:"
    r"\bwrite\b.{0,100}\b(?:script|program|code|\.py)\b"
    r"|\b(?:save|create)\b.{0,60}\b(?:script|\.py)\b"
    r"|\brun\b.{0,80}\b(?:script|it|the code|python|the program)\b"
    r"|\bpaste (?:the )?output\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# Model-facing tools for a photo-send turn. Host attaches the bytes.
CHAT_MEDIA_TOOLS: Tuple[str, ...] = ("web_search",)
CHAT_MEDIA_SUPPORTING_TOOLS: Tuple[str, ...] = ()
CHAT_MEDIA_BLOCKED_TOOLS: Tuple[str, ...] = (
    "generate_image",
    "spawn_agent",
    "capability_search",
    "run_command",
    "run_steps",
    "apply_workspace_changeset",
    "send_media",
    "google_search",
    "image_search",
    "deep_research",
    "browser_click",
    "browser_navigate",
    "browser_act",
    "browser_extract",
    "browser_snapshot",
    "browser_type",
    "browser_download",
)

MEDIA_DELIVERY_RULES = (
    "MEDIA DELIVERY (highest priority when they want a photo in this chat):\n"
    "If the user asks to send, download, find, fetch, show, or share a picture/"
    "photo into THIS chat, call `web_search(query=..., kind=\"images\")` once. "
    "Keep public-figure names in the query. Then STOP. The host downloads the "
    "best image and attaches it to the chat. Do not call `send_media`, "
    "`generate_image`, `spawn_agent`, `run_command`, or any browser tool. "
    "Do not open a search engine yourself.\n"
    "Do NOT call `generate_image` unless they asked to create, draw, render, or "
    "transform a NEW picture. Finding or downloading an existing photo is not "
    "image generation.\n"
    "The identity/avatar URL rule applies ONLY when the user is setting the "
    "bot's profile picture — not when they want a photo delivered into the chat.\n"
)


def is_write_and_run_request(text: str) -> bool:
    """True when the user wants a script written and executed, not a paste."""
    return bool(_WRITE_RUN_RE.search(text or ""))


def is_image_generation_request(text: str) -> bool:
    """True when the user wants a newly created/transformed picture."""
    return bool(_CREATE_RE.search(text or ""))


def is_chat_media_delivery(text: str) -> bool:
    """True when the user wants an existing photo/file delivered into chat."""
    blob = str(text or "").strip()
    if not blob or not _DELIVERY_RE.search(blob):
        return False
    if is_image_generation_request(blob) and not re.search(
        r"\b(?:download|find|fetch|busca(?:r)?|descarga(?:r)?)\b",
        blob,
        re.IGNORECASE,
    ):
        # "draw me a picture and send it" is generation; generate_image already
        # delivers. Fetch-and-send stays for download/find phrasing.
        return False
    return True


def exclusive_tools_for_turn(
    text: str, channel: str = ""
) -> Optional[FrozenSet[str]]:
    """Return an exclusive model-facing tool set for photo/generate turns."""
    blob = str(text or "").strip()
    if not blob:
        return None
    if is_write_and_run_request(blob):
        return None
    generation = is_image_generation_request(blob)
    delivery = is_chat_media_delivery(blob)
    if generation and not delivery:
        return frozenset({"generate_image"})
    if delivery and not generation:
        names = {"web_search"}
        # Discord/WhatsApp still expose send_media for non-web file share.
        # Web photo delivery is host-attached and must not register send_media.
        if str(channel or "").strip().lower() in {"discord", "whatsapp"}:
            names.add("send_media")
        return frozenset(names)
    return None
