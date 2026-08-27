"""
core/web_search.py
──────────────────
Host-owned search. The model calls ``web_search(kind=web|images|news)``.
The host fetches SERP HTML (Playwright internally), parses it, retries another
engine when a layout is empty, and returns structured ``SearchResponse`` data.

Engine names and browser clicks are not model tools. Missing Playwright fails
with ``BROWSER_INSTALL_HINT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Sequence
from urllib.parse import quote_plus

from core.search_parser import (
    parse_search_html,
    prepare_web_results,
    usable_image_url as parser_usable_image_url,
    usable_result_url,
)


DEFAULT_COUNT = 8
MAX_COUNT = 20
MIN_ORGANIC_WEB = 3

usable_image_url = parser_usable_image_url

FetchHtml = Callable[..., Awaitable[str]]


@dataclass
class SearchResult:
    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str = ""
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
    provider: str = "host"
    answer: str = ""
    results: List[SearchResult] = field(default_factory=list)
    images: List[ImageResult] = field(default_factory=list)
    error: str = ""
    attached: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.results or self.images or self.answer)


@dataclass(frozen=True)
class _Engine:
    name: str
    kind: str
    url: str
    scroll: bool = False


def _clamp_count(count: Any) -> int:
    try:
        return max(1, min(int(count), MAX_COUNT))
    except (TypeError, ValueError):
        return DEFAULT_COUNT


def _normalize_kind(kind: str) -> str:
    value = str(kind or "web").strip().lower()
    if value in {"image", "images", "photo", "photos", "pic", "pics"}:
        return "images"
    if value in {"news", "nws"}:
        return "news"
    return "web"


def search_urls(query: str, kind: str = "web") -> Sequence[_Engine]:
    """Internal engine list. Order is retry order."""
    encoded = quote_plus(str(query or "").strip())
    kind = _normalize_kind(kind)
    if kind == "images":
        return (
            _Engine(
                "bing_images",
                "images",
                f"https://www.bing.com/images/search?q={encoded}&form=HDRSC2",
                scroll=True,
            ),
            _Engine(
                "google_images",
                "images",
                f"https://www.google.com/search?tbm=isch&q={encoded}&hl=en&pws=0",
                scroll=True,
            ),
        )
    if kind == "news":
        return (
            _Engine(
                "google_news",
                "news",
                f"https://www.google.com/search?q={encoded}&hl=en&pws=0&tbm=nws",
            ),
            _Engine(
                "bing_news",
                "news",
                f"https://www.bing.com/news/search?q={encoded}",
            ),
        )
    return (
        _Engine(
            "google_web",
            "web",
            f"https://www.google.com/search?q={encoded}&hl=en&pws=0",
        ),
        _Engine(
            "bing_web",
            "web",
            f"https://www.bing.com/search?q={encoded}&setlang=en",
        ),
        _Engine(
            "ddg_web",
            "web",
            f"https://html.duckduckgo.com/html/?q={encoded}",
        ),
    )


def search_response_from_parsed(
    rows: Any,
    query: str,
    kind: str = "web",
    count: int = DEFAULT_COUNT,
    provider: str = "host",
) -> SearchResponse:
    limit = _clamp_count(count)
    kind = _normalize_kind(kind)
    resp = SearchResponse(kind=kind, query=query, provider=provider)
    if not isinstance(rows, list):
        resp.error = "search returned no data"
        return resp

    if kind == "images":
        for item in rows:
            if not isinstance(item, dict):
                continue
            image_url = usable_image_url(
                item.get("image_url") or item.get("url") or item.get("src")
            )
            if not image_url:
                continue
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
                    thumbnail_url=str(item.get("thumbnail_url") or ""),
                    source_page=usable_result_url(
                        item.get("source_page") or item.get("page_url") or ""
                    ),
                    width=width,
                    height=height,
                )
            )
            if len(resp.images) >= limit:
                break
        if not resp.images:
            resp.error = "no image results"
        return resp

    for item in prepare_web_results(rows, query, limit):
        resp.results.append(
            SearchResult(
                title=str(item.get("title") or item.get("url") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("snippet") or ""),
                source="host",
            )
        )
    if not resp.results:
        resp.error = "no organic results"
    return resp


def search_response_from_browser(
    raw: Any,
    query: str,
    kind: str = "web",
    count: int = DEFAULT_COUNT,
) -> SearchResponse:
    """Map a fetched HTML payload or a parsed dict onto SearchResponse."""
    if isinstance(raw, dict):
        kind = _normalize_kind(kind)
        rows = raw.get("images") if kind == "images" else raw.get("results")
        resp = search_response_from_parsed(
            rows or [], query=query, kind=kind, count=count, provider="host"
        )
        if not resp.ok:
            resp.error = str(raw.get("error") or resp.error or "no results")
        return resp
    if isinstance(raw, str):
        kind = _normalize_kind(kind)
        engine = "bing_images" if kind == "images" else "google_web"
        return search_response_from_parsed(
            parse_search_html(raw, engine, kind),
            query=query,
            kind=kind,
            count=count,
        )
    resp = SearchResponse(kind=_normalize_kind(kind), query=query, provider="host")
    resp.error = "search returned no data"
    return resp


def _web_results_sufficient(resp: SearchResponse, kind: str, count: int) -> bool:
    if kind == "images":
        return bool(resp.images)
    needed = min(MIN_ORGANIC_WEB, max(1, count))
    return len(resp.results) >= needed


def _merge_web_results(
    existing: List[SearchResult], incoming: List[SearchResult], query: str, limit: int
) -> List[SearchResult]:
    rows = [
        {"title": item.title, "url": item.url, "snippet": item.snippet}
        for item in existing + incoming
        if item.url
    ]
    merged = prepare_web_results(rows, query, limit)
    return [
        SearchResult(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            snippet=str(item.get("snippet") or ""),
            source="host",
        )
        for item in merged
    ]


async def run_host_search(
    query: str,
    kind: str = "web",
    count: int = DEFAULT_COUNT,
    fetch_html: FetchHtml | None = None,
) -> SearchResponse:
    """Fetch + parse + retry. ``fetch_html`` is injected (Playwright or tests).

    Empty SERPs, ads-only SERPs, and thin organic parses (fewer than
    ``MIN_ORGANIC_WEB`` web/news hits) trigger the next internal engine.
    Organic hits already found are kept and merged. The model is never told
    to open a search page.
    """
    query = str(query or "").strip()
    kind = _normalize_kind(kind)
    resp = SearchResponse(kind=kind, query=query, provider="host")
    if not query:
        resp.error = "a search query is required"
        return resp
    if fetch_html is None:
        resp.error = "search fetch is not configured"
        return resp

    last_error = ""
    accumulated: List[SearchResult] = []
    for engine in search_urls(query, kind):
        try:
            html = await fetch_html(engine.url, scroll=engine.scroll)
        except TypeError:
            try:
                html = await fetch_html(engine.url)
            except Exception as exc:
                last_error = str(exc)
                continue
        except Exception as exc:
            last_error = str(exc)
            continue
        parsed = parse_search_html(html or "", engine.name, kind)
        candidate = search_response_from_parsed(
            parsed, query=query, kind=kind, count=count, provider="host"
        )
        if kind == "images":
            if candidate.ok:
                return candidate
            last_error = candidate.error or f"{engine.name} returned no results"
            continue
        if candidate.results:
            accumulated = _merge_web_results(
                accumulated, candidate.results, query, _clamp_count(count)
            )
        if _web_results_sufficient(
            SearchResponse(kind=kind, query=query, results=list(accumulated)),
            kind,
            count,
        ):
            resp.results = accumulated
            return resp
        last_error = candidate.error or f"{engine.name} returned no organic results"

    if accumulated:
        resp.results = accumulated
        return resp
    resp.error = last_error or "no organic results"
    return resp


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def format_search_response(resp: SearchResponse) -> str:
    """Render a normalized SearchResponse into compact markdown for the LLM."""
    if resp.kind == "images":
        header = f"**Image search:** {resp.query} — {len(resp.images)} images"
        lines = [header, ""]
        for i, im in enumerate(resp.images, 1):
            title = im.title or "image"
            dims = f" ({im.width}x{im.height})" if im.width and im.height else ""
            lines.append(f"{i}. {title}{dims}")
            lines.append(f"   Image URL: {im.image_url}")
            if im.source_page:
                lines.append(f"   Source: {im.source_page}")
        lines.append("")
        if resp.attached:
            lines.append(
                "The host already attached the best image to this chat. "
                "Reply with a short caption. Do not call send_media or open a search page."
            )
        else:
            lines.append(
                "These are existing photos. For a chat photo request the host attaches "
                "the image; do not open a search engine or call generate_image."
            )
        return "\n".join(lines)

    label = "News search" if resp.kind == "news" else "Search"
    lines = [f"**{label}:** {resp.query}", ""]
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
