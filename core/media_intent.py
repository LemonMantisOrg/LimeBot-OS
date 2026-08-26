"""Intent helpers for chat media delivery vs image generation.

A request like "download a picture of X and send it in this chat" must route
to image_search + send_media. generate_image is only for newly created art.
"""

from __future__ import annotations

import re
from typing import Tuple

_DELIVERY_VERBS = (
    r"send|share|download|get|find|fetch|attach|look\s*up|look\s*for|"
    r"busca(?:r)?|busque|env[ií]a(?:me|nos)?|mand[ae]|descarga(?:r)?|"
    r"adjunt(?:a|ar)|muestra(?:me)?|show"
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
CHAT_MEDIA_TOOLS: Tuple[str, ...] = ("image_search", "send_media")
CHAT_MEDIA_SUPPORTING_TOOLS: Tuple[str, ...] = ("web_search",)
CHAT_MEDIA_BLOCKED_TOOLS: Tuple[str, ...] = (
    "generate_image",
    "spawn_agent",
    "capability_search",
    "run_command",
)

MEDIA_DELIVERY_RULES = (
    "MEDIA DELIVERY (highest priority when they want a photo/file in this chat):\n"
    "If the user asks to send, download, find, fetch, show, or share a picture/"
    "photo/file into THIS chat, call `image_search` (or `web_search` for a "
    "direct Wikimedia/image URL), then call `send_media(path=<Image URL>)` with "
    "a real http(s) image URL. Keep public-figure names in the search query. "
    "Add a short caption. Then STOP.\n"
    "Do NOT call `generate_image` unless they asked to create, draw, render, or "
    "transform a NEW picture. Finding or downloading an existing photo is not "
    "image generation.\n"
    "Do NOT call `spawn_agent`, `capability_search`, or `run_command`/curl for "
    "this. `send_media` already downloads the bytes (SSRF-guarded public HTTP) "
    "and attaches them to the outgoing chat message. No confirmation is required "
    "for `image_search` or `send_media`.\n"
    "The identity/avatar URL rule applies ONLY when the user is setting the "
    "bot's profile picture — not when they want a photo delivered into the chat.\n"
)


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
