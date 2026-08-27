import unittest
from pathlib import Path

from core.search_parser import (
    parse_bing_serp,
    parse_ddg_serp,
    parse_google_images,
    parse_google_serp,
    parse_bing_images,
    unwrap_redirect_url,
    usable_image_url,
    usable_result_url,
)
from core.web_search import run_host_search, search_response_from_parsed


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "search"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestSearchParserFixtures(unittest.TestCase):
    def test_google_web_returns_wikipedia_not_google_chrome(self):
        rows = parse_google_serp(_html("google_web.html"))
        urls = [row["url"] for row in rows]
        self.assertIn("https://en.wikipedia.org/wiki/Cat", urls)
        self.assertIn("https://www.aspca.org/pet-care/cat-care", urls)
        self.assertFalse(any("google.com" in url for url in urls))

    def test_google_consent_page_is_empty(self):
        self.assertEqual(parse_google_serp(_html("google_empty.html")), [])

    def test_bing_web_parses_algo_results(self):
        rows = parse_bing_serp(_html("bing_web.html"))
        self.assertEqual(rows[0]["url"], "https://en.wikipedia.org/wiki/Cat")
        self.assertEqual(rows[1]["url"], "https://www.britannica.com/animal/cat")

    def test_ddg_html_parses_result_links(self):
        rows = parse_ddg_serp(_html("ddg_web.html"))
        self.assertEqual(rows[0]["title"], "Cat - Wikipedia")

    def test_bing_images_keeps_originals_drops_thumbnails(self):
        rows = parse_bing_images(_html("bing_images.html"))
        urls = [row["image_url"] for row in rows]
        self.assertEqual(
            urls,
            [
                "https://cdn.example.test/rose.jpg",
                "https://img.example.test/jisoo.png",
            ],
        )
        self.assertEqual(usable_image_url("https://encrypted-tbn0.gstatic.com/x"), "")

    def test_google_images_unwraps_imgurl(self):
        rows = parse_google_images(_html("google_images.html"))
        urls = [row["image_url"] for row in rows]
        self.assertIn("https://img.example.test/rose.jpg", urls)
        self.assertIn("https://cdn.example.test/blackpink.png", urls)


class TestAdAndTrackingFilter(unittest.TestCase):
    def test_usable_result_url_drops_ad_and_tracking_links(self):
        dropped = [
            "https://www.bing.com/aclk?ld=ad1",
            "https://www.google.com/aclk?sa=L",
            "/aclk?sa=L",
            "https://www.googleadservices.com/pagead/aclk?adurl=https://spam.test",
            "https://googleads.g.doubleclick.net/aclk?sa=L",
            "https://ad.doubleclick.net/ddm/clk/123",
            "https://clk.msn.com/click",
            "https://yabs.yandex.ru/count/xyz",
        ]
        for url in dropped:
            with self.subTest(url=url):
                self.assertEqual(usable_result_url(url), "")

    def test_bing_organic_wrapper_unwraps_to_python_org(self):
        wrapped = (
            "https://www.bing.com/ck/a?!&&p=organic1"
            "&u=a1aHR0cHM6Ly93d3cucHl0aG9uLm9yZy9kb3dubG9hZHMv"
        )
        self.assertEqual(
            unwrap_redirect_url(wrapped),
            "https://www.python.org/downloads/",
        )
        self.assertEqual(
            usable_result_url(wrapped),
            "https://www.python.org/downloads/",
        )

    def test_aclk_is_not_unwrapped_into_an_advertiser_page(self):
        aclk = (
            "https://www.bing.com/aclk?ld=ad1"
            "&u=a1aHR0cHM6Ly93d3cucHl0aG9uLm9yZy9kb3dubG9hZHMv"
        )
        self.assertEqual(usable_result_url(aclk), "")
        self.assertEqual(unwrap_redirect_url(aclk), "")

    def test_ads_only_bing_serp_is_empty(self):
        rows = parse_bing_serp(_html("bing_ads_only.html"))
        self.assertEqual(rows, [])

    def test_ads_only_google_serp_is_empty(self):
        rows = parse_google_serp(_html("google_ads_only.html"))
        self.assertEqual(rows, [])

    def test_mixed_bing_serp_keeps_python_org_and_strips_ads(self):
        rows = parse_bing_serp(_html("bing_mixed_python.html"))
        urls = [row["url"] for row in rows]
        self.assertIn("https://www.python.org/downloads/", urls)
        self.assertIn("https://docs.python.org/3/whatsnew/3.13.html", urls)
        self.assertTrue(any("realpython.example.test" in url for url in urls))
        self.assertFalse(any("aclk" in url for url in urls))
        self.assertFalse(any("ads.example.test" in url for url in urls))
        self.assertFalse(any("bing.com" in url for url in urls))
        python = next(row for row in rows if "python.org/downloads" in row["url"])
        self.assertIn("official home", python["snippet"].lower())


class TestNewsQualityRanking(unittest.TestCase):
    def test_mixed_news_serp_keeps_world_desks_drops_school_assembly(self):
        rows = parse_bing_serp(_html("bing_news_mixed.html"))
        urls = [row["url"] for row in rows]
        self.assertIn("https://www.reuters.com/world/top-news-today/", urls)
        self.assertIn("https://www.bbc.com/news/world-123", urls)
        self.assertTrue(any("jagranjosh.com" in url for url in urls))
        self.assertFalse(any("apiclick" in url for url in urls))

        resp = search_response_from_parsed(
            rows, query="top world news headlines", kind="news", count=8
        )
        self.assertTrue(resp.ok)
        ranked = [item.url for item in resp.results]
        self.assertIn("https://www.reuters.com/world/top-news-today/", ranked)
        self.assertIn("https://www.bbc.com/news/world-123", ranked)
        self.assertTrue(
            ranked[0].startswith("https://www.reuters.com/")
            or ranked[0].startswith("https://www.bbc.com/")
        )
        self.assertFalse(any("jagranjosh.com" in url for url in ranked))
        self.assertFalse(any("abplive.com" in url for url in ranked))
        self.assertFalse(any("msn.com" in url for url in ranked))
        self.assertFalse(any("BingNewsVerp" in url for url in ranked))

    def test_yahoo_mashable_roundups_dropped_reuters_bbc_kept(self):
        rows = parse_bing_serp(_html("bing_news_roundups.html"))
        resp = search_response_from_parsed(
            rows, query="top world news headlines", kind="news", count=8
        )
        ranked = [item.url for item in resp.results]
        self.assertIn("https://www.reuters.com/world/top-news-today/", ranked)
        self.assertIn("https://www.bbc.com/news/world-123", ranked)
        self.assertFalse(any("yahoo.com" in url for url in ranked))
        self.assertFalse(any("mashable.com" in url for url in ranked))
        self.assertTrue(
            ranked[0].startswith("https://www.reuters.com/")
            or ranked[0].startswith("https://www.bbc.com/")
        )

    def test_prepare_web_results_drops_school_assembly_only_for_news(self):
        from core.search_parser import prepare_web_results

        rows = [
            {
                "title": "Top 10 World News Headlines Today for School Assembly",
                "url": "https://www.jagranjosh.com/general-knowledge/school-assembly",
                "snippet": "Current affairs for school assembly and exam prep.",
            },
            {
                "title": "World news",
                "url": "https://www.reuters.com/world/top-news-today/",
                "snippet": "Reuters world desk headlines.",
            },
            {
                "title": "Recycled world roundup",
                "url": (
                    "https://www.msn.com/en-us/news/world/recycled-headline/"
                    "ar-AA1junk?ocid=BingNewsVerp"
                ),
                "snippet": "MSN BingNewsVerp wrapper.",
            },
        ]
        news = prepare_web_results(rows, "top world news headlines", 8, kind="news")
        news_urls = [item["url"] for item in news]
        self.assertEqual(news_urls, ["https://www.reuters.com/world/top-news-today/"])

        web = prepare_web_results(rows, "jagran josh school assembly", 8, kind="web")
        web_urls = [item["url"] for item in web]
        self.assertTrue(any("jagranjosh.com" in url for url in web_urls))
        self.assertTrue(any("reuters.com" in url for url in web_urls))

    def test_bing_news_apiclick_unwraps_to_bbc(self):
        wrapped = (
            "https://www.bing.com/news/apiclick.aspx?"
            "url=https%3A%2F%2Fwww.bbc.com%2Fnews%2Fworld-123"
        )
        self.assertEqual(
            unwrap_redirect_url(wrapped),
            "https://www.bbc.com/news/world-123",
        )
        self.assertEqual(
            usable_result_url(wrapped),
            "https://www.bbc.com/news/world-123",
        )

    def test_whats_new_python_prefers_docs_over_whatsapp_wikipedia(self):
        rows = parse_bing_serp(_html("bing_python_whatsnew.html"))
        resp = search_response_from_parsed(
            rows, query="What's New in Python 3.14", kind="web", count=8
        )
        self.assertTrue(resp.ok)
        urls = [item.url for item in resp.results]
        self.assertTrue(urls[0].startswith("https://docs.python.org/3/whatsnew/3.14"))
        self.assertFalse(any("whatsapp.com" in url for url in urls))

    def test_live_fx_drops_history_and_keeps_html_rate_page(self):
        rows = parse_bing_serp(_html("bing_fx_mixed.html"))
        resp = search_response_from_parsed(
            rows, query="live USD/GTQ exchange rate", kind="web", count=8
        )
        self.assertTrue(resp.ok)
        urls = [item.url for item in resp.results]
        self.assertTrue(any("exchanging.com" in url for url in urls))
        self.assertTrue(urls[0].startswith("https://www.exchanging.com/"))
        self.assertFalse(any("exchange-rates.org" in url for url in urls))
        self.assertFalse(any("xe.com" in url for url in urls))
        self.assertFalse(any("/history" in url for url in urls))
        self.assertIn("7.63", resp.results[0].snippet)
        self.assertIn("7.63", resp.answer)

    def test_empty_xe_spa_html_has_no_rate_html_rate_is_kept(self):
        from core.search_parser import fx_rate_from_html

        self.assertIsNone(
            fx_rate_from_html(_html("xe_spa.html"), "live USD GTQ rate")
        )
        self.assertEqual(
            fx_rate_from_html(_html("fx_rate_html.html"), "live USD GTQ rate"),
            7.63,
        )

    def test_ranked_response_puts_python_org_ahead_of_blogs_and_ads(self):
        rows = parse_bing_serp(_html("bing_mixed_python.html"))
        resp = search_response_from_parsed(
            rows, query="current Python release", kind="web", count=8
        )
        self.assertTrue(resp.ok)
        urls = [item.url for item in resp.results]
        self.assertGreaterEqual(len(urls), 3)
        self.assertTrue(urls[0].startswith("https://www.python.org/"))
        self.assertIn("https://docs.python.org/3/whatsnew/3.13.html", urls)
        self.assertFalse(any("aclk" in url for url in urls))

    def test_dedupes_www_and_bare_host_on_the_same_path(self):
        from core.search_parser import prepare_web_results

        rows = [
            {
                "title": "Download Python",
                "url": "https://www.python.org/downloads/",
                "snippet": "Official",
            },
            {
                "title": "Download Python again",
                "url": "https://python.org/downloads",
                "snippet": "Duplicate",
            },
        ]
        out = prepare_web_results(rows, "current Python release", 8)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["url"].rstrip("/").endswith("python.org/downloads"))


class TestHostSearchRetry(unittest.IsolatedAsyncioTestCase):
    async def test_empty_google_retries_bing(self):
        google_html = _html("google_empty.html")
        bing_html = _html("bing_web.html")

        async def fetch_html(url: str, scroll: bool = False) -> str:
            if "google.com" in url:
                return google_html
            if "bing.com" in url:
                return bing_html
            return ""

        resp = await run_host_search("cats", kind="web", count=5, fetch_html=fetch_html)
        self.assertTrue(resp.ok)
        self.assertEqual(resp.provider, "host")
        self.assertEqual(resp.results[0].url, "https://en.wikipedia.org/wiki/Cat")

    async def test_ads_only_google_retries_bing_and_keeps_python_org(self):
        fetched = []

        async def fetch_html(url: str, scroll: bool = False) -> str:
            fetched.append(url)
            if "google.com" in url:
                return _html("google_ads_only.html")
            if "bing.com" in url:
                return _html("bing_mixed_python.html")
            return _html("ddg_web.html")

        resp = await run_host_search(
            "current Python release", kind="web", count=8, fetch_html=fetch_html
        )
        urls = [item.url for item in resp.results]
        self.assertTrue(resp.ok)
        self.assertTrue(any("python.org" in url for url in urls))
        self.assertTrue(urls[0].startswith("https://www.python.org/"))
        self.assertFalse(any("aclk" in url for url in urls))
        self.assertFalse(any("doubleclick" in url for url in urls))
        self.assertGreaterEqual(len(fetched), 2)

    async def test_news_roundups_retry_until_world_desks(self):
        from core.search_parser import is_world_news_desk

        fetched = []

        async def fetch_html(url: str, scroll: bool = False) -> str:
            fetched.append(url)
            if "google.com" in url:
                return _html("google_news_roundups.html")
            if "bing.com" in url:
                return _html("bing_news_desks.html")
            return ""

        resp = await run_host_search(
            "top world news headlines", kind="news", count=8, fetch_html=fetch_html
        )
        urls = [item.url for item in resp.results]
        self.assertTrue(resp.ok)
        self.assertFalse(any("yahoo.com" in url for url in urls))
        self.assertFalse(any("mashable.com" in url for url in urls))
        self.assertGreaterEqual(sum(1 for url in urls if is_world_news_desk(url)), 3)
        self.assertGreaterEqual(len(fetched), 2)

    async def test_fx_empty_spa_retries_html_rate_source(self):
        fetched = []

        async def fetch_html(url: str, scroll: bool = False) -> str:
            fetched.append(url)
            if "google.com" in url:
                return _html("google_fx_xe.html")
            if "xe.com" in url:
                return _html("xe_spa.html")
            if "bing.com" in url:
                return _html("bing_fx_oanda.html")
            if "oanda.com" in url:
                return _html("fx_rate_html.html")
            return ""

        resp = await run_host_search(
            "live USD/GTQ exchange rate convert Q1000",
            kind="web",
            count=8,
            fetch_html=fetch_html,
        )
        self.assertTrue(resp.ok)
        self.assertIn("7.63", resp.answer)
        self.assertTrue(any("oanda.com" in item.url for item in resp.results))
        self.assertFalse(any("xe.com" in item.url for item in resp.results))
        self.assertGreaterEqual(len(fetched), 2)
        self.assertTrue(any("google.com" in url for url in fetched))
        self.assertTrue(any("bing.com" in url for url in fetched))
        self.assertFalse(any("xe.com" in url for url in fetched))

    async def test_fx_html_page_fills_rate_when_snippet_has_no_number(self):
        fetched = []

        async def fetch_html(url: str, scroll: bool = False) -> str:
            fetched.append(url)
            if "google.com" in url:
                return _html("google_fx_oanda_no_rate.html")
            if "oanda.com" in url:
                return _html("fx_rate_html.html")
            if "bing.com" in url:
                return _html("bing_fx_oanda.html")
            return ""

        resp = await run_host_search(
            "live USD GTQ rate",
            kind="web",
            count=8,
            fetch_html=fetch_html,
        )
        self.assertTrue(resp.ok)
        self.assertIn("7.63", resp.answer)
        self.assertTrue(any("oanda.com" in item.url for item in resp.results))
        self.assertTrue(any("oanda.com" in url for url in fetched))
        self.assertIn("7.63", resp.results[0].snippet)

    async def test_ads_only_everywhere_fails_without_browser_instruction(self):
        async def fetch_html(url: str, scroll: bool = False) -> str:
            if "google.com" in url:
                return _html("google_ads_only.html")
            return _html("bing_ads_only.html")

        resp = await run_host_search(
            "current Python release", kind="web", count=8, fetch_html=fetch_html
        )
        self.assertFalse(resp.ok)
        self.assertIn("organic", resp.error.lower())
        self.assertNotIn("open google", resp.error.lower())
        self.assertNotIn("browser_navigate", resp.error.lower())

    async def test_image_search_uses_bing_fixture(self):
        async def fetch_html(url: str, scroll: bool = False) -> str:
            if "bing.com/images" in url:
                return _html("bing_images.html")
            return ""

        resp = await run_host_search(
            "rose blackpink", kind="images", count=8, fetch_html=fetch_html
        )
        self.assertTrue(resp.ok)
        self.assertEqual(resp.images[0].image_url, "https://cdn.example.test/rose.jpg")

    def test_parsed_response_drops_empty_image_lists(self):
        resp = search_response_from_parsed([], query="x", kind="images")
        self.assertFalse(resp.ok)
        self.assertIn("no image", resp.error)


if __name__ == "__main__":
    unittest.main()
