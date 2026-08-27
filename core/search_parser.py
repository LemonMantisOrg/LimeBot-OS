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

# World desks ranked first for kind=news. Retry until at least three of these hit.
_NEWS_WORLD_DESKS = (
    "reuters.com",
    "apnews.com",
    "ap.org",
    "bbc.com",
    "bbc.co.uk",
    "afp.com",
    "nytimes.com",
    "washingtonpost.com",
    "theguardian.com",
    "aljazeera.com",
    "ft.com",
    "wsj.com",
    "bloomberg.com",
    "npr.org",
    "dw.com",
    "france24.com",
)

# Optional regional desks: keep and rank, but do not count toward the retry floor.
_NEWS_REGIONAL_DESKS = (
    "thehindu.com",
    "abc.net.au",
)

# School-assembly, exam-prep, lifestyle aggregators, and leftover chrome.
_NEWS_JUNK_HOSTS = (
    "jagranjosh.com",
    "abplive.com",
    "yahoo.com",
    "mashable.com",
    "buzzfeed.com",
    "buzzfeednews.com",
    "byjus.com",
    "vedantu.com",
    "unacademy.com",
    "testbook.com",
    "adda247.com",
    "gradeup.co",
    "careerpower.in",
    "affairscloud.com",
    "bankersadda.com",
    "sscadda.com",
    "studyiq.com",
    "oliveboard.in",
    "examrace.com",
    "gktoday.in",
)

# Leftover click chrome after unwrap. Real desks reached via url= are kept.
_NEWS_WRAPPER_HOSTS = (
    "msn.com",
    "bing.com",
    "news.google.com",
    "flipboard.com",
    "smartnews.com",
)

_NEWS_JUNK_TEXT = (
    "school assembly",
    "school-assembly",
    "morning assembly",
    "for school students",
    "exam prep",
    "exam-prep",
    "current affairs for",
    "competitive exam",
    "bank exam",
    "ssc cgl",
    "ibps po",
    "upsc ",
    "gk quiz",
    "gk today",
    "daily current affairs",
    "catching our eye",
    "caught our eye",
    "catch our eye",
    "news roundup",
    "headlines roundup",
    "stories roundup",
    "daily roundup",
    "weekly roundup",
    "morning roundup",
)

_FX_PREFERRED_HOSTS = (
    "oanda.com",
    "x-rates.com",
    "exchangerates.org.uk",
    "exchanging.com",
    "wise.com",
    "ofx.com",
    "investing.com",
    "bloomberg.com",
    "reuters.com",
    "ft.com",
    "wsj.com",
    "open.er-api.com",
    "exchangerate.host",
    "frankfurter.app",
)

# JS-heavy converters whose first HTML snapshot is often an empty shell.
_FX_SPA_HOSTS = (
    "xe.com",
)

_FX_HISTORY_MARKERS = (
    "/history",
    "/historical",
    "historical-rate",
    "historical_rate",
    "history-of",
    "/hist/",
)

_FX_HISTORY_TEXT = (
    "historical exchange",
    "exchange rate history",
    "history of the",
    "historical rates",
    "rates in 20",
)

# Landing pages that often render a default EUR/USD (or similar) rate.
_FX_GENERIC_CONVERTER_HOSTS = (
    "calculator.net",
)

_ISO_CURRENCY_RE = re.compile(
    r"\b(usd|eur|gbp|jpy|gtq|mxn|cad|aud|chf|cny|hkd|inr|brl|krw|"
    r"nzd|sek|nok|dkk|pln|try|zar|sgd|thb|php|idr)\b",
    re.I,
)

_CURRENCY_ALIASES = {
    "usd": (r"us\s*dollar", r"u\.s\.\s*dollar", r"united states dollar"),
    "gtq": (r"quetzales?", r"guatemalan quetzal"),
    "eur": (r"\beuros?\b",),
}


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
    if "bing.com" in host and "/aclk" not in path:
        if (
            path.startswith("/ck/")
            or path.startswith("/news/apiclick")
            or "apiclick.aspx" in path
        ):
            return True
    if "msn.com" in host and (
        "apiclick" in path or "linkredir" in path or path.startswith("/click")
    ):
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


def _normalized_host(url: str) -> str:
    host = (urlparse(str(url or "")).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_in(host: str, names: tuple[str, ...]) -> bool:
    return any(host == name or host.endswith("." + name) for name in names)


def result_identity(url: str) -> str:
    """Registrable-host + path identity used to dedupe organic cards."""
    host = _normalized_host(url)
    path = (urlparse(str(url or "")).path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def official_result_boost(query: str, url: str) -> int:
    """Higher scores win. Official software hosts beat blogs and leftovers."""
    host = _normalized_host(url)
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


def is_python_docs_query(query: str) -> bool:
    q = " ".join(str(query or "").lower().split())
    if "python" not in q:
        return False
    if any(marker in q for marker in ("what's new", "whats new", "whatsnew", "documentation")):
        return True
    if re.search(r"\b3\.\d{1,2}\b", q) and any(
        marker in q for marker in ("new", "feature", "docs", "library", "whats")
    ):
        return True
    return False


def python_docs_boost(query: str, url: str) -> int:
    if not is_python_docs_query(query):
        return 0
    host = _normalized_host(url)
    path = (urlparse(url).path or "").lower()
    if host in {"whatsapp.com", "wa.me"} or host.endswith(".whatsapp.com"):
        return -200
    if host in {"wikipedia.org"} or host.endswith(".wikipedia.org"):
        return -60
    if host == "docs.python.org" or host.endswith(".docs.python.org"):
        score = 120
        if "/whatsnew" in path:
            score += 40
        version = re.search(r"\b3\.\d{1,2}\b", str(query or ""))
        if version and version.group(0) in path:
            score += 20
        return score
    if host == "python.org" or host.endswith(".python.org"):
        return 50
    return 0


def is_official_python_docs_url(url: str) -> bool:
    host = _normalized_host(url)
    path = (urlparse(url).path or "").lower()
    if host == "docs.python.org" or host.endswith(".docs.python.org"):
        return True
    if (host == "python.org" or host.endswith(".python.org")) and "/whatsnew" in path:
        return True
    return False


def is_fx_query(query: str) -> bool:
    q = str(query or "").lower()
    if re.search(r"\b(?:usd|eur|gbp|jpy|gtq|mxn|cad|fx|forex)\b", q):
        return True
    if re.search(r"\bq\s*\d{2,}", q) or "quetzal" in q:
        return True
    return bool(re.search(r"exchange\s+rate|currency|convert\b.{0,40}\b(?:to|from)\b", q))


def is_fx_spa_host(url: str) -> bool:
    return _host_in(_normalized_host(url), _FX_SPA_HOSTS)


def is_fx_generic_converter_host(url: str) -> bool:
    return _host_in(_normalized_host(url), _FX_GENERIC_CONVERTER_HOSTS)


def fx_query_currencies(query: str) -> frozenset[str]:
    """ISO codes the user asked to convert, e.g. USD+GTQ from 'USD to GTQ' or Q1000."""
    q = re.sub(r"[/\-_]", " ", str(query or "").lower())
    found = {code.lower() for code in _ISO_CURRENCY_RE.findall(q)}
    if re.search(r"quetzal", q) or re.search(r"\bq\s*\d{2,}", q):
        found.add("gtq")
    if re.search(r"\beuros?\b", q):
        found.add("eur")
    if re.search(r"us\s*dollar|u\.s\.\s*dollar|united states dollar", q):
        found.add("usd")
    return frozenset(found)


def _fx_text_has_currency(blob: str, code: str, wanted: frozenset[str] | None = None) -> bool:
    text = str(blob or "").lower()
    if re.search(rf"\b{re.escape(code)}\b", text):
        return True
    for pattern in _CURRENCY_ALIASES.get(code, ()):
        if re.search(pattern, text):
            return True
    if code == "usd" and wanted and "usd" in wanted and re.search(r"\bdollars?\b", text):
        return True
    return False


def fx_text_has_query_pair(blob: str, query: str) -> bool:
    wanted = fx_query_currencies(query)
    if len(wanted) < 2:
        return True
    text = str(blob or "").lower()
    return all(_fx_text_has_currency(text, code, wanted) for code in wanted)


def fx_result_matches_pair(
    query: str, url: str, title: str = "", snippet: str = ""
) -> bool:
    return fx_text_has_query_pair(f"{url} {title} {snippet}", query)


def is_fx_off_pair_result(
    query: str, url: str, title: str = "", snippet: str = ""
) -> bool:
    """True when a converter card is not the query pair (e.g. EUR/USD for USD/GTQ)."""
    if not is_fx_query(query):
        return False
    wanted = fx_query_currencies(query)
    if len(wanted) < 2:
        return False
    return not fx_result_matches_pair(query, url, title, snippet)


def fx_rate_from_text(text: str, query: str = "") -> float | None:
    """Return the first plausible live FX rate in visible text, or None.

    Integers such as Q1000 and years are ignored. A USD/GTQ query only accepts
    a 6.5–9.5 GTQ-per-USD figure from text that names both currencies — never a
    leftover EUR/USD default such as 1.366.
    """
    blob = str(text or "")
    if not blob:
        return None
    wanted = fx_query_currencies(query)
    if len(wanted) >= 2 and not fx_text_has_query_pair(blob, query):
        return None
    matches = [
        float(match.group(1))
        for match in re.finditer(r"(?<![\d.])(\d{1,2}\.\d{2,6})(?![\d])", blob)
    ]
    if not matches:
        return None
    if "gtq" in wanted or _fx_text_has_currency(blob, "gtq", wanted):
        gtq = [value for value in matches if 6.5 <= value <= 9.5]
        return gtq[0] if gtq else None
    for value in matches:
        if 0.10 <= value <= 99.99:
            return value
    return None


def fx_rate_from_html(html: str, query: str = "") -> float | None:
    """Parse a visible FX rate from HTML, ignoring script/style SPA shells."""
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return fx_rate_from_text(soup.get_text(" ", strip=True), query)


def fx_empty_extract_note(text: str, url: str) -> str:
    """Host hint when a converter extract has no usable USD/GTQ rate."""
    host = _normalized_host(url)
    path = (urlparse(str(url or "")).path or "").lower()
    looks_like_fx = (
        is_fx_spa_host(url)
        or is_fx_generic_converter_host(url)
        or _host_in(host, _FX_PREFERRED_HOSTS)
        or "currency" in path
        or "converter" in path
        or "exchange" in path
        or is_fx_query(str(url or ""))
    )
    if not looks_like_fx:
        return ""
    blob = f"{url} {text}".lower()
    has_gtq = _fx_text_has_currency(blob, "gtq")
    has_eur = _fx_text_has_currency(blob, "eur")
    if has_eur and not has_gtq:
        return (
            "This snapshot is not USD/GTQ (likely a default EUR/USD calculator). "
            "Do not use this number. Call web_search for USD GTQ or "
            "browser_navigate an HTML USD/GTQ page (oanda, x-rates, exchanging)."
        )
    if fx_rate_from_text(f"{url} {text}", "USD GTQ") is not None:
        return ""
    return (
        "No numeric FX rate in this snapshot (likely a JS shell). Do not refuse. "
        "Call web_search for USD GTQ and use a snippet rate, or browser_navigate a "
        "different HTML current-rate page (oanda, x-rates, exchanging)."
    )


def is_fx_history_result(url: str, title: str = "", snippet: str = "") -> bool:
    lowered = f"{url} {title} {snippet}".lower()
    path = (urlparse(url).path or "").lower()
    if any(marker in path or marker in lowered for marker in _FX_HISTORY_MARKERS):
        return True
    if any(marker in lowered for marker in _FX_HISTORY_TEXT):
        return True
    if re.search(r"/20(0\d|1\d)(/|$)", path):
        return True
    return False


def fx_result_boost(query: str, url: str, title: str = "", snippet: str = "") -> int:
    if not is_fx_query(query):
        return 0
    if is_fx_history_result(url, title, snippet):
        return -200
    if is_fx_off_pair_result(query, url, title, snippet):
        return -200
    host = _normalized_host(url)
    path = (urlparse(url).path or "").lower()
    score = 0
    if is_fx_spa_host(url):
        score -= 80
    if is_fx_generic_converter_host(url):
        score -= 80
    if _host_in(host, _FX_PREFERRED_HOSTS):
        score += 90
    if fx_rate_from_text(f"{url} {title} {snippet}", query) is not None:
        score += 80
    if any(marker in path for marker in ("/converter", "/convert", "/live", "usd-", "-usd", "gtq")):
        score += 20
    if "gtq" in f"{url} {title} {snippet}".lower() or "quetzal" in f"{title} {snippet}".lower():
        score += 30
    return score


def is_world_news_desk(url: str) -> bool:
    return _host_in(_normalized_host(url), _NEWS_WORLD_DESKS)


def news_result_boost(url: str) -> int:
    """World desks outrank leftover aggregators for kind=news."""
    host = _normalized_host(url)
    if _host_in(host, _NEWS_WORLD_DESKS):
        return 100
    if _host_in(host, _NEWS_REGIONAL_DESKS):
        return 40
    return 0


def is_news_junk_result(url: str, title: str = "", snippet: str = "") -> bool:
    """Drop school-assembly, exam-prep, Yahoo/Mashable roundups, and MSN chrome."""
    host = _normalized_host(url)
    if _host_in(host, _NEWS_JUNK_HOSTS) or _host_in(host, _NEWS_WRAPPER_HOSTS):
        return True
    blob = f"{title} {snippet} {url}".lower()
    if any(marker in blob for marker in _NEWS_JUNK_TEXT):
        return True
    if "bingnewservp" in blob or "apiclick.aspx" in blob:
        return True
    title_l = str(title or "").lower()
    if "roundup" in title_l and not is_world_news_desk(url):
        return True
    return False


def should_drop_web_result(
    query: str, url: str, title: str = "", snippet: str = "", kind: str = "web"
) -> bool:
    kind = str(kind or "web").strip().lower()
    if kind == "news":
        return is_news_junk_result(url, title, snippet)
    if is_fx_query(query) and is_fx_history_result(url, title, snippet):
        return True
    if is_fx_query(query) and is_fx_off_pair_result(query, url, title, snippet):
        return True
    if is_python_docs_query(query):
        host = _normalized_host(url)
        if host in {"whatsapp.com"} or host.endswith(".whatsapp.com"):
            return True
    return False


def query_result_boost(
    query: str, url: str, title: str = "", snippet: str = "", kind: str = "web"
) -> int:
    score = official_result_boost(query, url)
    if str(kind or "web").strip().lower() == "news":
        score += news_result_boost(url)
    score += python_docs_boost(query, url)
    score += fx_result_boost(query, url, title, snippet)
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


def rank_web_results(
    rows: List[Dict[str, Any]], query: str, kind: str = "web"
) -> List[Dict[str, Any]]:
    def _key(item: tuple[int, Dict[str, Any]]) -> tuple:
        idx, row = item
        url = str(row.get("url") or "")
        title = str(row.get("title") or "")
        snippet = str(row.get("snippet") or "")
        return (
            -query_result_boost(query, url, title, snippet, kind),
            idx,
        )

    indexed = list(enumerate(rows))
    indexed.sort(key=_key)
    return [row for _, row in indexed]


def prepare_web_results(
    rows: Iterable[Dict[str, Any]], query: str, limit: int, kind: str = "web"
) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        url = usable_result_url(item.get("url"))
        if not url:
            continue
        title = str(item.get("title") or url)
        snippet = str(item.get("snippet") or "")[:600]
        if should_drop_web_result(query, url, title, snippet, kind=kind):
            continue
        if is_fx_query(query):
            rate = fx_rate_from_text(f"{url} {title} {snippet}", query)
            if rate is not None and "live rate" not in snippet.lower():
                if "gtq" in fx_query_currencies(query):
                    snippet = (
                        f"Live rate ≈ {rate} (GTQ per 1 USD). {snippet}"
                    ).strip()
                else:
                    snippet = f"Live rate ≈ {rate}. {snippet}".strip()
        cleaned.append({"title": title, "url": url, "snippet": snippet})
    cleaned = rank_web_results(dedupe_web_results(cleaned), query, kind=kind)
    if is_fx_query(query) and any(
        fx_rate_from_text(f"{row['title']} {row['snippet']}", query) is not None
        for row in cleaned
    ):
        cleaned = [
            row
            for row in cleaned
            if not (
                is_fx_spa_host(row["url"])
                and fx_rate_from_text(
                    f"{row['title']} {row['snippet']}", query
                )
                is None
            )
        ]
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
