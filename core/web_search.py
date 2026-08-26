"""
core/web_search.py
──────────────────
Normalize Playwright search results into ``SearchResponse``.

``web_search`` / ``image_search`` / ``deep_research`` drive the real browser
(Google for web/news, Bing Images with a Google Images fallback). There is no
HTTP search-API chain — no Tavily, Brave Search API, SerpAPI, or DuckDuckGo
i.js. Missing Playwright fails with ``BROWSER_INSTALL_HINT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List
from urllib.parse import urlparse


DEFAULT_COUNT = 8
MAX_COUNT = 20

_THUMBNAIL_MARKERS = (
    "encrypted-tbn",
    "gstatic.com/images",
    "google.com/images/branding",
    "/th?id=",
    "th.bing.com/th?",
)


# ── Normalized result types ──────────────────────────────────────────────


@dataclass
class SearchResult:
    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str = ""  # filled later by deep_research via fetch_readable_text
    published: str = ""
    source: str = ""


@dataclass
class ImageResult:
    title: str = ""
    image_url: str = ""
    thumbnail_url: str = ""
    source_page: str = ""
    width: int = 0
    height: int = 0


@dataclass
class SearchResponse:
    kind: str = "web"  # web | news | images
    query: str = ""
    provider: str = ""
    answer: str = ""
    results: List[SearchResult] = field(default_factory=list)
    images: List[ImageResult] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.results or self.images or self.answer)


def _clamp_count(count: Any) -> int:
    try:
        return max(1, min(int(count), MAX_COUNT))
    except (TypeError, ValueError):
        return DEFAULT_COUNT


def _http_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(url)
    if not parsed.netloc:
        return ""
    return url


def usable_image_url(value: Any) -> str:
    """Return a remote image URL the model can pass to send_media, or empty."""
    url = _http_url(value)
    if not url:
        return ""
    lowered = url.lower()
    if lowered.startswith("data:") or lowered.startswith("blob:"):
        return ""
    if any(marker in lowered for marker in _THUMBNAIL_MARKERS):
        return ""
    return url


def search_response_from_browser(
    raw: Any,
    query: str,
    kind: str = "web",
    count: int = DEFAULT_COUNT,
) -> SearchResponse:
    """Map a BrowserManager search payload onto SearchResponse."""
    limit = _clamp_count(count)
    kind = "images" if kind == "images" else ("news" if kind == "news" else "web")
    resp = SearchResponse(kind=kind, query=query, provider="browser")
    if not isinstance(raw, dict):
        resp.error = "browser search returned no data"
        return resp

    raw_error = str(raw.get("error") or "").strip()

    if kind == "images":
        for item in raw.get("images") or []:
            if not isinstance(item, dict):
                continue
            image_url = usable_image_url(
                item.get("image_url") or item.get("url") or item.get("src")
            )
            if not image_url:
                continue
            thumb = usable_image_url(item.get("thumbnail_url")) or _http_url(
                item.get("thumbnail_url")
            )
            try:
                width = int(item.get("width") or 0)
            except (TypeError, ValueError):
                width = 0
            try:
                height = int(item.get("height") or 0)
            except (TypeError, ValueError):
                height = 0
            resp.images.append(
                ImageResult(
                    title=str(item.get("title") or item.get("alt") or ""),
                    image_url=image_url,
                    thumbnail_url=thumb,
                    source_page=_http_url(item.get("source_page") or item.get("page_url")),
                    width=width,
                    height=height,
                )
            )
            if len(resp.images) >= limit:
                break
        if not resp.images:
            resp.error = raw_error or "no image results"
        return resp

    for item in raw.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = _http_url(item.get("url"))
        if not url:
            continue
        resp.results.append(
            SearchResult(
                title=str(item.get("title") or url),
                url=url,
                snippet=str(item.get("snippet") or ""),
                source="browser",
            )
        )
        if len(resp.results) >= limit:
            break
    if not resp.results:
        resp.error = raw_error or "no search results"
    return resp


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def format_search_response(resp: SearchResponse) -> str:
    """Render a normalized SearchResponse into compact markdown for the LLM."""
    if resp.kind == "images":
        header = (
            f"**Image search:** {resp.query} (via {resp.provider}) — "
            f"{len(resp.images)} images"
        )
        lines = [header, ""]
        for i, im in enumerate(resp.images, 1):
            title = im.title or "image"
            dims = f" ({im.width}x{im.height})" if im.width and im.height else ""
            lines.append(f"{i}. {title}{dims}")
            lines.append(f"   Image URL: {im.image_url}")
            if im.source_page:
                lines.append(f"   Source: {im.source_page}")
        lines.append("")
        lines.append(
            "Tip: to deliver one to the user, call "
            "send_media(path='<Image URL>') — it will fetch and attach it."
        )
        return "\n".join(lines)

    label = "News search" if resp.kind == "news" else "Search"
    lines = [f"**{label}:** {resp.query} (via {resp.provider})", ""]
    if resp.answer:
        lines.append(f"**Direct answer:** {_truncate(resp.answer, 800)}")
        lines.append("")
    for i, r in enumerate(resp.results, 1):
        lines.append(f"{i}. {r.title or r.url}")
        lines.append(f"   URL: {r.url}")
        meta = _truncate(r.snippet, 300)
        if r.published:
            meta = f"({r.published}) {meta}".strip()
        if meta:
            lines.append(f"   {meta}")
    return "\n".join(lines)
