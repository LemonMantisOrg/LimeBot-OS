"""Host-owned SERP parsers.

Playwright (or a test double) only fetches HTML. This module turns that HTML
into structured organic results. Ads, click-wrappers, and engine chrome are
stripped here so the model never sees them. Engine names stay internal.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup


# Leftover engine chrome after unwrap. Organic destinations must not match.
_SKIP_HOST_MARKERS = (
    "gstatic.com",
    "googleusercontent.com",
    "g.page",
    "youtube.com/results",
    "bing.com/images/search",
    "microsoft.com/en-us/bing",
    "duckduckgo.com/y.js",
    "duckduckgo.com/l.js",
)

_AD_HOST_MARKERS = (
    "googleadservices.",
    "googlesyndication.",
    "doubleclick.",
    "googleads.",
    "pagead2.",
    "adservice.",
    "clk.msn.com",
    "c.msn.com",
    "s.msn.com",
    "yabs.yandex",
    "an.yandex.",
    "yandexadexchange.",
    "yandex.ru/an/",
    "adclick.g.doubleclick",
)

_AD_PATH_MARKERS = (
    "/aclk",
    "/pagead/aclk",
    "/aclick",
    "/ck/a",
    "/ck/ad",
)

_THUMBNAIL_MARKERS = (
    "encrypted-tbn",
    "gstatic.com/images",
    "google.com/images/branding",
    "/th?id=",
    "th.bing.com/th?",
)

_AD_CONTAINER_IDS = {"tads", "tadsb", "bottomads", "b_ad"}
_AD_CONTAINER_CLASSES = (
    "b_ad",
    "b_adlastchild",
    "sb_add",
    "b_adsl",
    "ueierd",
    "ads-ad",
    "commercial-unit",
    "commercial-unit-desktop-top",
    "cu-container",
    "sponsored-result",
)

# Query-token → official hosts. Used to rank version/release lookups.
_OFFICIAL_HOSTS = {
    "python": ("python.org", "docs.python.org", "peps.python.org", "pypi.org"),
    "node": ("nodejs.org", "npmjs.com"),
    "nodejs": ("nodejs.org", "npmjs.com"),
    "javascript": ("developer.mozilla.org", "tc39.es"),
    "typescript": ("typescriptlang.org",),
    "rust": ("rust-lang.org", "doc.rust-lang.org"),
    "go": ("go.dev", "golang.org"),
    "golang": ("go.dev", "golang.org"),
    "java": ("oracle.com", "docs.oracle.com", "openjdk.org"),
    "ruby": ("ruby-lang.org", "docs.ruby-lang.org"),
    "php": ("php.net",),
    "dotnet": ("learn.microsoft.com", "dot.net", "microsoft.com"),
    "linux": ("kernel.org",),
    "ubuntu": ("ubuntu.com",),
    "debian": ("debian.org",),
    "kubernetes": ("kubernetes.io",),
    "docker": ("docker.com", "docs.docker.com"),
}

_RELEASE_PATH_MARKERS = (
    "/download",
    "/downloads",
    "/release",
    "/releases",
    "/whatsnew",
    "/changelog",
    "/news/release",
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


def _is_ad_click_url(url: str) -> bool:
    """True for ad/tracking click URLs that must never reach the model."""
    raw = str(url or "").strip().lower()
    if not raw:
        return False
    if "/aclk" in raw or "aclk?" in raw:
        return True
    parsed = urlparse(raw if "://" in raw else f"https://placeholder.invalid{raw}")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if any(marker in host or marker in raw for marker in _AD_HOST_MARKERS):
        return True
    if any(marker in path for marker in ("/aclk", "/aclick")):
        return True
    if "bing.com" in host and any(marker in path for marker in ("/aclk", "/adredir")):
        return True
    return False


def _is_organic_wrapper(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if path.startswith("/url") or "imgres" in path:
        return True
    if "bing.com" in host and path.startswith("/ck/") and "/aclk" not in path:
        return True
    if "duckduckgo.com" in host and ("uddg" in (parsed.query or "") or path.startswith("/l/")):
        return True
    return False


def _decode_embedded_url(value: str) -> str:
    raw = unquote(str(value or "").strip())
    if not raw:
        return ""
    direct = _http_url(raw)
    if direct:
        return direct
    # Bing ck/a stores the destination as a1 + base64(url).
    payload = raw
    if payload.startswith(("a1", "a0")) and len(payload) > 4:
        payload = payload[2:]
    if payload.startswith("aHR0"):
        padded = payload + "=" * ((4 - len(payload) % 4) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode(
                "utf-8", "ignore"
            )
        except Exception:
            decoded = ""
        return _http_url(decoded)
    return ""


def unwrap_redirect_url(url: str) -> str:
    """Unwrap Google /url?q=, Bing ck/a, and DDG uddg wrappers.

    Ad click URLs (aclk, doubleclick, googleadservices) are not unwrapped into
    advertiser landing pages — they are dropped by usable_result_url.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    if _is_ad_click_url(raw):
        return ""
    absolute = _http_url(raw) or (
        _http_url(urljoin("https://www.google.com", raw)) if raw.startswith("/") else ""
    )
    if absolute and _is_ad_click_url(absolute):
        return ""
    parsed = urlparse(absolute or raw)
    query = parse_qs(parsed.query)
    for key in ("q", "url", "imgurl", "mediaurl", "murl", "uddg", "udsg"):
        candidate = (query.get(key) or [""])[0]
        unwrapped = _decode_embedded_url(candidate) or _http_url(unquote(candidate))
        if unwrapped and not _is_ad_click_url(unwrapped):
            return unwrapped
    # Bing organic click wrapper.
    if _is_organic_wrapper(absolute or raw):
        for key in ("u", "u3", "r", "ru"):
            candidate = (query.get(key) or [""])[0]
            unwrapped = _decode_embedded_url(candidate)
            if unwrapped and not _is_ad_click_url(unwrapped):
                return unwrapped
    if parsed.path.startswith("/url") or "imgres" in parsed.path:
        for key in ("q", "imgurl"):
            candidate = (query.get(key) or [""])[0]
            unwrapped = _http_url(unquote(candidate))
            if unwrapped:
                return unwrapped
    result = absolute if absolute and absolute.startswith(("http://", "https://")) else ""
    if result and _is_ad_click_url(result):
        return ""
    if result and _is_organic_wrapper(result):
        return ""
    return result


def _host_is_skipped(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    lowered = url.lower()
    if "google." in host:
        return True
    if any(marker in host or marker in lowered for marker in _SKIP_HOST_MARKERS):
        return True
    if "bing.com" in host and any(marker in urlparse(url).path.lower() for marker in _AD_PATH_MARKERS):
        return True
    return False


def usable_result_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or _is_ad_click_url(raw):
        return ""
    url = unwrap_redirect_url(raw)
    if not url or _is_ad_click_url(url) or _host_is_skipped(url):
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


def result_identity(url: str) -> str:
    """Registrable-host + path identity used to dedupe organic cards."""
    parsed = urlparse(str(url or ""))
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def official_result_boost(query: str, url: str) -> int:
    """Higher scores win. Official software hosts beat blogs and leftovers."""
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (urlparse(url).path or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    score = 0
    for token, hosts in _OFFICIAL_HOSTS.items():
        if token not in tokens:
            continue
        if any(host == h or host.endswith("." + h) for h in hosts):
            score += 80
    host_labels = set(host.split("."))
    if tokens & host_labels:
        score += 25
    if any(marker in path for marker in _RELEASE_PATH_MARKERS):
        score += 15
    if path in {"", "/"} and score:
        score += 5
    return score


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


def dedupe_web_results(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        url = str(row.get("url") or "")
        ident = result_identity(url)
        if not ident or ident in seen:
            continue
        seen.add(ident)
        out.append(row)
    return out


def rank_web_results(rows: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    indexed = list(enumerate(rows))
    indexed.sort(
        key=lambda item: (-official_result_boost(query, str(item[1].get("url") or "")), item[0])
    )
    return [row for _, row in indexed]


def prepare_web_results(
    rows: Iterable[Dict[str, Any]], query: str, limit: int
) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        url = usable_result_url(item.get("url"))
        if not url:
            continue
        cleaned.append(
            {
                "title": str(item.get("title") or url),
                "url": url,
                "snippet": str(item.get("snippet") or "")[:600],
            }
        )
    cleaned = rank_web_results(dedupe_web_results(cleaned), query)
    if limit > 0:
        return cleaned[:limit]
    return cleaned


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _is_ad_node(node) -> bool:
    current = node
    hops = 0
    while current is not None and hops < 12:
        hops += 1
        name = getattr(current, "name", None)
        if name is None:
            break
        ident = (current.get("id") or "").lower()
        classes = " ".join(current.get("class") or []).lower()
        aria = (current.get("aria-label") or "").lower()
        data_tag = (current.get("data-tag") or current.get("data-ad") or "").lower()
        if ident in _AD_CONTAINER_IDS:
            return True
        if any(token in classes.split() or token in classes for token in _AD_CONTAINER_CLASSES):
            return True
        if "sponsored" in classes or "sponsored" in aria or aria in {"ads", "ad"}:
            return True
        if data_tag in {"ad", "ads", "sponsored"}:
            return True
        current = getattr(current, "parent", None)
    return False


def _cite_to_url(node) -> str:
    text = _text(node)
    if not text:
        return ""
    text = text.replace(" › ", "/").replace("»", "/").replace(" > ", "/")
    token = text.split()[0].strip(".:")
    if token.startswith(("http://", "https://")):
        return token
    if "." in token and not token.startswith("javascript:"):
        return "https://" + token.lstrip("/")
    return ""


def parse_google_serp(html: str) -> List[Dict[str, Any]]:
    """Parse a Google web/news results page, skipping ad units."""
    soup = _soup(html)
    results: List[Dict[str, Any]] = []

    for link in soup.select("a"):
        if _is_ad_node(link):
            continue
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
        return dedupe_web_results(results)

    for container in soup.select("div.g, div[data-sokoban-container], div.MjjYud"):
        if _is_ad_node(container):
            continue
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
    return dedupe_web_results(results)


def parse_bing_serp(html: str) -> List[Dict[str, Any]]:
    """Parse Bing web or news HTML. Ads and aclk wrappers are dropped."""
    soup = _soup(html)
    results: List[Dict[str, Any]] = []
    for item in soup.select("li.b_algo, div.news-card, div.newsitem"):
        if _is_ad_node(item):
            continue
        link = item.select_one("h2 a, a.title")
        if link is None:
            continue
        href = str(link.get("href") or "")
        if _is_ad_click_url(href):
            continue
        url = usable_result_url(href)
        if not url:
            url = usable_result_url(_cite_to_url(item.select_one("cite")))
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
        return dedupe_web_results(results)

    for link in soup.select("ol#b_results h2 a, #b_results h2 a"):
        if _is_ad_node(link):
            continue
        url = usable_result_url(link.get("href") or "")
        title = _text(link)
        if url and title:
            results.append({"title": title, "url": url, "snippet": ""})
    return dedupe_web_results(results)


def parse_ddg_serp(html: str) -> List[Dict[str, Any]]:
    """Parse the keyless DuckDuckGo HTML endpoint."""
    soup = _soup(html)
    results: List[Dict[str, Any]] = []
    for link in soup.select("a.result__a, a.result-link"):
        if _is_ad_node(link):
            continue
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
    return dedupe_web_results(results)


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
