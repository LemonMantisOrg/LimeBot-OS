"""Host helper for public Instagram post stills.

Instagram carousels are not in ``og:image``. The public embed document at
``/p/{shortcode}/embed/captioned/`` nests ``edge_sidecar_to_children`` JSON
inside the HTML. This module parses that sidecar, skips profile pics, and
downloads stills with an Instagram Referer. It is not a login scraper.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional
from urllib.parse import urlparse

INSTAGRAM_REFERER = "https://www.instagram.com/"
EMBED_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 "
    "Safari/604.1"
)
PROFILE_PIC_MARKER = "t51.82787-19"

_POST_PATH_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


@dataclass(frozen=True)
class SidecarNode:
    display_url: str
    is_video: bool
    shortcode: str = ""

    @property
    def is_profile_pic(self) -> bool:
        return PROFILE_PIC_MARKER in (self.display_url or "")

    @property
    def is_still(self) -> bool:
        return bool(self.display_url) and not self.is_video and not self.is_profile_pic


@dataclass
class InstagramCarousel:
    shortcode: str
    nodes: List[SidecarNode] = field(default_factory=list)
    photo_paths: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def photo_count(self) -> int:
        return sum(1 for node in self.nodes if node.is_still)

    @property
    def video_count(self) -> int:
        return sum(1 for node in self.nodes if node.is_video)

    def summary(self) -> str:
        if self.error:
            return self.error
        paths = ", ".join(self.photo_paths) if self.photo_paths else "(none downloaded)"
        return (
            f"Instagram post {self.shortcode}: {len(self.nodes)} sidecar slide(s), "
            f"{self.photo_count} photo(s), {self.video_count} video slide(s). "
            f"Photo paths: {paths}. Do not treat og:image as the carousel."
        )


def instagram_shortcodes(text: str) -> List[str]:
    """Return distinct shortcodes from instagram.com/p/ or /reel/ URLs."""
    found: List[str] = []
    seen = set()
    for match in _POST_PATH_RE.finditer(text or ""):
        code = match.group(1)
        if code not in seen:
            seen.add(code)
            found.append(code)
    return found


def is_instagram_post_url(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    return bool(_POST_PATH_RE.search(raw))


def embed_url(shortcode: str) -> str:
    code = str(shortcode or "").strip()
    return f"https://www.instagram.com/p/{code}/embed/captioned/"


def _decode_unicode_escapes(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return match.group(0)

    return _UNICODE_ESCAPE_RE.sub(_replace, text)


def _unescape_embed_html(text: str) -> str:
    """Turn Instagram's nested/escaped embed JSON into parseable text."""
    blob = html.unescape(text or "")
    # Embed pages often double-escape quotes, slashes, and \\uXXXX sequences.
    blob = blob.replace(r"\\u", r"\u")
    blob = _decode_unicode_escapes(blob)
    blob = blob.replace(r"\/", "/").replace(r"\\/", "/")
    blob = blob.replace(r"\"", '"').replace(r'\\"', '"')
    blob = blob.replace(r"\%", "%")
    return blob


def _loads_sidecar_json(blob: str) -> Optional[Any]:
    candidates = [blob]
    cleaned = (
        blob.replace(r"\/", "/")
        .replace(r"\\/", "/")
        .replace(r"\%", "%")
        .replace(r"\\u", r"\u")
    )
    if cleaned != blob:
        candidates.append(cleaned)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _slice_json_object(text: str, start: int) -> str:
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    in_str = False
    esc = False
    for index in range(start, len(text)):
        char = text[index]
        if in_str:
            if esc:
                esc = False
            elif char == "\\":
                esc = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _nodes_from_sidecar_payload(payload: Any) -> List[SidecarNode]:
    if not isinstance(payload, dict):
        return []
    edges = payload.get("edges")
    if not isinstance(edges, list):
        return []
    nodes: List[SidecarNode] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node") if isinstance(edge.get("node"), dict) else edge
        if not isinstance(node, dict):
            continue
        url = str(node.get("display_url") or "").strip()
        if not url:
            continue
        is_video = node.get("is_video")
        if isinstance(is_video, str):
            is_video = is_video.strip().lower() in {"1", "true", "yes"}
        nodes.append(
            SidecarNode(
                display_url=url,
                is_video=bool(is_video),
                shortcode=str(node.get("shortcode") or ""),
            )
        )
    return nodes


def parse_sidecar_nodes(html_text: str) -> List[SidecarNode]:
    """Parse ``edge_sidecar_to_children`` nodes from an embed HTML document.

    Instagram nests/escapes this JSON inside the page. ``og:image`` is ignored
    because a carousel's Open Graph tag is one video thumbnail.
    """
    if not html_text:
        return []
    for source in (html_text, _unescape_embed_html(html_text)):
        key_at = source.find("edge_sidecar_to_children")
        if key_at < 0:
            continue
        brace_at = source.find("{", key_at)
        blob = _slice_json_object(source, brace_at)
        if not blob:
            continue
        payload = _loads_sidecar_json(blob)
        if payload is None:
            continue
        nodes = _nodes_from_sidecar_payload(payload)
        if nodes:
            return nodes
    return []


FetchBytes = Callable[..., Awaitable[bytes]]
SaveStill = Callable[[int, bytes, str], Awaitable[str]]


async def fetch_carousel_stills(
    shortcode: str,
    *,
    fetch_bytes: FetchBytes,
    save_still: SaveStill,
) -> InstagramCarousel:
    """Fetch the public embed, parse sidecar stills, and save photos to disk."""
    code = str(shortcode or "").strip()
    result = InstagramCarousel(shortcode=code)
    if not code:
        result.error = "Error: Instagram shortcode is required."
        return result
    embed = embed_url(code)
    try:
        body = await fetch_bytes(
            embed,
            headers={
                "User-Agent": EMBED_USER_AGENT,
                "Referer": INSTAGRAM_REFERER,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
    except Exception as exc:
        result.error = f"Error: Could not fetch Instagram embed: {exc}"
        return result
    html_text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or "")
    result.nodes = parse_sidecar_nodes(html_text)
    if not result.nodes:
        result.error = (
            "Error: Instagram embed had no sidecar stills. "
            "Do not fall back to og:image; that is one video thumbnail, not the carousel."
        )
        return result
    stills = [node for node in result.nodes if node.is_still]
    if not stills:
        return result
    paths: List[str] = []
    for index, node in enumerate(stills):
        try:
            image_bytes = await fetch_bytes(
                node.display_url,
                headers={
                    "User-Agent": EMBED_USER_AGENT,
                    "Referer": INSTAGRAM_REFERER,
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                },
            )
            if not image_bytes:
                continue
            path = await save_still(index, image_bytes, node.display_url)
            if path and not str(path).startswith("Error:"):
                paths.append(path)
        except Exception:
            continue
    result.photo_paths = paths
    if not paths:
        result.error = (
            f"Error: Parsed {result.photo_count} Instagram photo(s) but none downloaded."
        )
    return result
