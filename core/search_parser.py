"""Host-owned SERP parsers.

Playwright (or a test double) only fetches HTML. This module turns that HTML
into structured results. Engine names stay internal — they are never a model
tool and must not appear in user-facing tool schemas.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup


_SKIP_HOST_MARKERS = (
    "google.",
    "gstatic.com",
    "googleusercontent.com",
    "g.page",
    "youtube.com/results",
    "bing.com/ck/",
    "bing.com/aclick",
    "bing.com/images/search",
    "microsoft.com/en-us/bing",
    "duckduckgo.com/y.js",
    "duckduckgo.com/l.js",
)

_THUMBNAIL_MARKERS = (
    "encrypted-tbn",
    "gstatic.com/images",
    "google.com/images/branding",
    "/th?id=",
    "th.bing.com/th?",
)


def _http_url(value: Any, base: str = "") -> str:
    url = str(value or "").strip()
    if not url or url.startswith(("#", "javascript:", "data:", "blob:")):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if base and not url.startswith(("http://", "https://")):
        url = urljoin(base, url)
    if not url.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(url)
    if not parsed.netloc:
        return ""
    return url


def unwrap_redirect_url(url: str) -> str:
    """Unwrap Google /url?q= and similar redirect wrappers."""
    raw = _http_url(url) or str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    query = parse_qs(parsed.query)
    for key in ("q", "url", "imgurl", "mediaurl", "murl"):
        candidate = (query.get(key) or [""])[0]
        unwrapped = _http_url(unquote(candidate))
        if unwrapped:
            return unwrapped
    if parsed.path.startswith("/url") or "imgres" in parsed.path:
        for key in ("q", "imgurl"):
            candidate = (query.get(key) or [""])[0]
            unwrapped = _http_url(unquote(candidate))
            if unwrapped:
                return unwrapped
    return raw if raw.startswith(("http://", "https://")) else ""


def _host_is_skipped(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    lowered = url.lower()
    if any(marker in host or marker in lowered for marker in _SKIP_HOST_MARKERS):
        return True
    return False


def usable_result_url(value: Any) -> str:
    url = unwrap_redirect_url(str(value or ""))
    if not url or _host_is_skipped(url):
        return ""
    return url


def usable_image_url(value: Any) -> str:
    url = unwrap_redirect_url(str(value or ""))
    if not url:
        return ""
    lowered = url.lower()
    if any(marker in lowered for marker in _THUMBNAIL_MARKERS):
        return ""
    if _host_is_skipped(url) and "imgurl=" not in lowered:
        return ""
    return url


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _dedupe(rows: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        value = str(row.get(key) or "")
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(row)
    return out


def parse_google_serp(html: str) -> List[Dict[str, Any]]:
    """Parse a Google web/news results page."""
    soup = _soup(html)
    results: List[Dict[str, Any]] = []

    for link in soup.select("a"):
        heading = link.find("h3")
        if heading is None:
            continue
        url = usable_result_url(link.get("href") or "")
        title = _text(heading)
        if not url or not title:
            continue
        snippet = ""
        container = link.find_parent("div")
        for _ in range(6):
            if container is None:
                break
            blob = _text(container)
            if title and blob.startswith(title):
                snippet = blob[len(title) :].strip()
                break
            container = container.find_parent("div")
        results.append({"title": title, "url": url, "snippet": snippet[:600]})

    if results:
        return _dedupe(results, "url")

    for container in soup.select("div.g, div[data-sokoban-container], div.MjjYud"):
        heading = container.find("h3")
        link = container.find("a")
        if heading is None or link is None:
            continue
        url = usable_result_url(link.get("href") or "")
        title = _text(heading)
        if not url or not title:
            continue
        snippet_el = container.select_one("div.VwiC3b, div[data-sncf], span.st")
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": _text(snippet_el)[:600],
            }
        )
    return _dedupe(results, "url")


def parse_bing_serp(html: str) -> List[Dict[str, Any]]:
    """Parse Bing web or news HTML."""
    soup = _soup(html)
    results: List[Dict[str, Any]] = []
    for item in soup.select("li.b_algo, div.news-card, div.newsitem"):
        link = item.select_one("h2 a, a.title")
        if link is None:
            continue
        url = usable_result_url(link.get("href") or "")
        title = _text(link)
        if not url or not title:
            continue
        snippet_el = item.select_one("p, div.b_caption p, div.snippet")
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": _text(snippet_el)[:600],
            }
        )
    if results:
        return _dedupe(results, "url")

    for link in soup.select("ol#b_results h2 a, #b_results h2 a"):
        url = usable_result_url(link.get("href") or "")
        title = _text(link)
        if url and title:
            results.append({"title": title, "url": url, "snippet": ""})
    return _dedupe(results, "url")


def parse_ddg_serp(html: str) -> List[Dict[str, Any]]:
    """Parse the keyless DuckDuckGo HTML endpoint."""
    soup = _soup(html)
    results: List[Dict[str, Any]] = []
    for link in soup.select("a.result__a, a.result-link"):
        url = usable_result_url(link.get("href") or "")
        title = _text(link)
        if not url or not title:
            continue
        parent = link.find_parent("div")
        snippet_el = None
        if parent is not None:
            snippet_el = parent.select_one("a.result__snippet, .result__snippet")
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": _text(snippet_el)[:600],
            }
        )
    return _dedupe(results, "url")


def _bing_image_from_m(raw: str) -> Dict[str, Any] | None:
    if not raw:
        return None
    try:
        meta = json.loads(raw)
    except Exception:
        try:
            meta = json.loads(raw.replace("&quot;", '"'))
        except Exception:
            return None
    if not isinstance(meta, dict):
        return None
    image_url = usable_image_url(meta.get("murl") or meta.get("mediaurl") or "")
    if not image_url:
        return None
    thumb = str(meta.get("turl") or "")
    try:
        width = int(meta.get("w") or 0)
    except (TypeError, ValueError):
        width = 0
    try:
        height = int(meta.get("h") or 0)
    except (TypeError, ValueError):
        height = 0
    return {
        "title": str(meta.get("t") or ""),
        "image_url": image_url,
        "thumbnail_url": thumb,
        "source_page": usable_result_url(meta.get("purl") or "") or "",
        "width": width,
        "height": height,
    }


def parse_bing_images(html: str) -> List[Dict[str, Any]]:
    soup = _soup(html)
    images: List[Dict[str, Any]] = []
    for node in soup.select("a.iusc, a[m], .iusc"):
        parsed = _bing_image_from_m(node.get("m") or "")
        if parsed:
            images.append(parsed)
    if images:
        return _dedupe(images, "image_url")

    # Fallback: murl in inline JSON / attributes.
    for match in re.finditer(r'"murl"\s*:\s*"(https?:[^"\\]+)"', html or ""):
        image_url = usable_image_url(match.group(1).encode("utf-8").decode("unicode_escape"))
        if image_url:
            images.append(
                {
                    "title": "",
                    "image_url": image_url,
                    "thumbnail_url": "",
                    "source_page": "",
                    "width": 0,
                    "height": 0,
                }
            )
    return _dedupe(images, "image_url")


def parse_google_images(html: str) -> List[Dict[str, Any]]:
    soup = _soup(html)
    images: List[Dict[str, Any]] = []
    for link in soup.select('a[href*="imgurl="], a[href*="/imgres"]'):
        href = link.get("href") or ""
        parsed = urlparse(urljoin("https://www.google.com", href))
        query = parse_qs(parsed.query)
        image_url = usable_image_url((query.get("imgurl") or [""])[0])
        if not image_url:
            continue
        img = link.find("img")
        title = ""
        if img is not None:
            title = str(img.get("alt") or img.get("title") or "")
        title = title or link.get("aria-label") or ""
        images.append(
            {
                "title": title,
                "image_url": image_url,
                "thumbnail_url": (img.get("src") if img is not None else "") or "",
                "source_page": usable_result_url((query.get("imgrefurl") or [""])[0]),
                "width": 0,
                "height": 0,
            }
        )

    for match in re.finditer(r'"ou"\s*:\s*"(https?:[^"\\]+)"', html or ""):
        image_url = usable_image_url(match.group(1).encode("utf-8").decode("unicode_escape"))
        if image_url:
            images.append(
                {
                    "title": "",
                    "image_url": image_url,
                    "thumbnail_url": "",
                    "source_page": "",
                    "width": 0,
                    "height": 0,
                }
            )
    return _dedupe(images, "image_url")


def parse_search_html(html: str, engine: str, kind: str = "web") -> List[Dict[str, Any]]:
    """Dispatch HTML to the parser for one internal engine."""
    engine = str(engine or "").strip().lower()
    kind = str(kind or "web").strip().lower()
    if kind == "images":
        if engine.startswith("google"):
            return parse_google_images(html)
        return parse_bing_images(html)
    if engine.startswith("bing"):
        return parse_bing_serp(html)
    if engine.startswith("ddg") or engine.startswith("duck"):
        return parse_ddg_serp(html)
    return parse_google_serp(html)
