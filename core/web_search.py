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
from urllib.parse import quote_plus, urlparse

from core.search_parser import (
    fx_rate_from_html,
    fx_rate_from_text,
    is_fx_query,
    is_fx_spa_host,
    is_official_python_docs_url,
    is_python_docs_query,
    is_world_news_desk,
    parse_search_html,
    prepare_web_results,
    usable_image_url as parser_usable_image_url,
    usable_result_url,
)


DEFAULT_COUNT = 8
MAX_COUNT = 20
MIN_ORGANIC_WEB = 3
MIN_WORLD_DESK_NEWS = 3
MAX_FX_PAGE_FETCHES = 3

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

    for item in prepare_web_results(rows, query, limit, kind=kind):
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
    _attach_fx_answer(resp)
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


def _world_desk_count(results: List[SearchResult]) -> int:
    return sum(1 for item in results if is_world_news_desk(item.url))


def _fx_rate_from_result(query: str, item: SearchResult) -> float | None:
    return fx_rate_from_text(
        f"{item.title} {item.snippet} {item.content}", query
    )


def _attach_fx_answer(resp: SearchResponse) -> None:
    if not is_fx_query(resp.query):
        return
    for item in resp.results:
        rate = _fx_rate_from_result(resp.query, item)
        if rate is None:
            continue
        resp.answer = (
            f"Live rate ≈ {rate} from {item.url}. "
            f"Compute Q1000 / {rate} with calculate when converting quetzales. "
            "Cite this URL and a timestamp. Never refuse a live rate."
        )
        return


def _fx_destination_fetchable(url: str) -> bool:
    raw = str(url or "")
    if not raw.startswith(("http://", "https://")):
        return False
    host = (urlparse(raw).netloc or "").lower()
    if "google." in host or "bing.com" in host or "duckduckgo.com" in host:
        return False
    return True


async def _hydrate_fx_rates(
    results: List[SearchResult],
    query: str,
    fetch_html: FetchHtml | None,
) -> List[SearchResult]:
    """Fill live rates from snippets, or by fetching HTML that actually has a number."""
    if not is_fx_query(query):
        return results
    hydrated: List[SearchResult] = []
    fetches = 0
    found_rate = any(_fx_rate_from_result(query, item) is not None for item in results)
    for item in results:
        rate = _fx_rate_from_result(query, item)
        snippet = item.snippet
        content = item.content
        if (
            rate is None
            and not found_rate
            and fetch_html is not None
            and fetches < MAX_FX_PAGE_FETCHES
            and _fx_destination_fetchable(item.url)
            and not is_fx_spa_host(item.url)
        ):
            fetches += 1
            try:
                html = await fetch_html(item.url)
            except TypeError:
                try:
                    html = await fetch_html(item.url, scroll=False)
                except Exception:
                    html = ""
            except Exception:
                html = ""
            rate = fx_rate_from_html(html or "", query)
            if rate is not None:
                found_rate = True
        if rate is not None:
            content = str(rate)
            if "live rate" not in snippet.lower():
                snippet = f"Live rate ≈ {rate}. {snippet}".strip()
        hydrated.append(
            SearchResult(
                title=item.title,
                url=item.url,
                snippet=snippet,
                content=content,
                published=item.published,
                source=item.source or "host",
            )
        )
    return hydrated


def _web_results_sufficient(
    resp: SearchResponse, kind: str, count: int, query: str = ""
) -> bool:
    if kind == "images":
        return bool(resp.images)
    needed = min(MIN_ORGANIC_WEB, max(1, count))
    if kind == "news":
        return _world_desk_count(resp.results) >= min(MIN_WORLD_DESK_NEWS, max(1, count))
    if is_fx_query(query):
        return any(_fx_rate_from_result(query, item) is not None for item in resp.results)
    if is_python_docs_query(query):
        return any(is_official_python_docs_url(item.url) for item in resp.results)
    return len(resp.results) >= needed


def _merge_web_results(
    existing: List[SearchResult],
    incoming: List[SearchResult],
    query: str,
    limit: int,
    kind: str = "web",
) -> List[SearchResult]:
    rows = [
        {"title": item.title, "url": item.url, "snippet": item.snippet}
        for item in existing + incoming
        if item.url
    ]
    merged = prepare_web_results(rows, query, limit, kind=kind)
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
    ``MIN_ORGANIC_WEB`` web hits, or fewer than ``MIN_WORLD_DESK_NEWS``
    world-desk news hits) trigger the next internal engine. Python docs and
    live FX queries also retry until an official/current-rate page appears.
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
            if is_fx_query(query):
                candidate.results = await _hydrate_fx_rates(
                    list(candidate.results), query, fetch_html
                )
            accumulated = _merge_web_results(
                accumulated, candidate.results, query, _clamp_count(count), kind=kind
            )
        if _web_results_sufficient(
            SearchResponse(kind=kind, query=query, results=list(accumulated)),
            kind,
            count,
            query=query,
        ):
            resp.results = accumulated
            _attach_fx_answer(resp)
            return resp
        last_error = candidate.error or f"{engine.name} returned no organic results"

    if accumulated:
        resp.results = accumulated
        _attach_fx_answer(resp)
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
    if resp.kind == "news":
        lines.append("")
        lines.append(
            "Prefer a world-desk article URL (Reuters, AP, BBC, AFP, NYT, "
            "Washington Post, Guardian, Al Jazeera, FT, WSJ, Bloomberg, NPR, DW, "
            "France 24). browser_navigate that article then browser_extract and "
            "quote the first sentence; do not paraphrase the lede."
        )
    elif is_fx_query(resp.query):
        lines.append("")
        if resp.answer:
            lines.append(
                "Use that live rate. CALL calculate for the conversion "
                "(Q1000 / rate for quetzales to USD). Cite the source URL and timestamp. "
                "Never refuse."
            )
        else:
            lines.append(
                "No numeric rate in these snippets yet. Retry web_search or open an HTML "
                "current-rate page (oanda, x-rates, exchanging) — not a JS-only Xe shell "
                "and not a history URL. CALL calculate once you have the rate. "
                "Never refuse."
            )
    return "\n".join(lines)
