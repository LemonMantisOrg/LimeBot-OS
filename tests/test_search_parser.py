import unittest
from pathlib import Path

from core.search_parser import (
    parse_bing_images,
    parse_bing_serp,
    parse_ddg_serp,
    parse_google_images,
    parse_google_serp,
    usable_image_url,
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
