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
