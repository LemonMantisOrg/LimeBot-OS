import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class TestFormatting(unittest.TestCase):
    def test_web_results_formatted_with_urls(self):
        from core.web_search import (
            SearchResponse,
            SearchResult,
            format_search_response,
        )

        resp = SearchResponse(kind="web", query="cats", provider="host")
        resp.answer = "Cats are mammals."
        resp.results = [SearchResult(title="Cats", url="https://ex.test/cats", snippet="Feline")]
        out = format_search_response(resp)
        self.assertIn("https://ex.test/cats", out)
        self.assertIn("Direct answer", out)
        self.assertNotIn("Tavily", out)
        self.assertNotIn("DuckDuckGo", out)
        self.assertNotIn("open google.com", out.lower())

    def test_image_results_do_not_instruct_send_media(self):
        from core.web_search import (
            SearchResponse,
            ImageResult,
            format_search_response,
        )

        resp = SearchResponse(kind="images", query="pup", provider="host")
        resp.images = [
            ImageResult(title="Puppy", image_url="https://img.test/a.jpg", source_page="https://p.test")
        ]
        out = format_search_response(resp)
        self.assertIn("https://img.test/a.jpg", out)
        self.assertIn("Image URL:", out)
        self.assertNotIn("call send_media", out)
        self.assertNotIn("open google.com", out.lower())

        resp.attached = True
        attached = format_search_response(resp)
        self.assertIn("already attached", attached)
        self.assertIn("Do not call send_media", attached)

    def test_news_results_ask_for_world_desk_lede_quote(self):
        from core.web_search import SearchResponse, SearchResult, format_search_response

        resp = SearchResponse(kind="news", query="top world headlines", provider="host")
        resp.results = [
            SearchResult(title="World", url="https://www.reuters.com/world/", snippet="Desk")
        ]
        out = format_search_response(resp)
        self.assertIn("browser_navigate", out)
        self.assertIn("first sentence", out)
        self.assertIn("world-desk", out.lower())

    def test_fx_results_forbid_history_and_refusal(self):
        from core.web_search import SearchResponse, SearchResult, format_search_response

        resp = SearchResponse(
            kind="web", query="live USD/GTQ exchange rate convert Q1000", provider="host"
        )
        resp.results = [
            SearchResult(title="XE", url="https://www.xe.com/currencyconverter/", snippet="Live")
        ]
        out = format_search_response(resp)
        self.assertIn("current-rate", out)
        self.assertIn("history", out)
        self.assertIn("refuse", out.lower())
        self.assertIn("calculate", out)
        self.assertNotIn("check Xe.com yourself", out)
        self.assertNotIn("check the site themselves", out.lower())

    def test_fx_snippet_rate_is_surfaced_as_direct_answer(self):
        from core.web_search import search_response_from_parsed, format_search_response

        resp = search_response_from_parsed(
            [
                {
                    "title": "USD to GTQ",
                    "url": "https://www.exchanging.com/usd-gtq",
                    "snippet": "1 USD = 7.63 GTQ. Updated today.",
                }
            ],
            query="live USD/GTQ convert Q1000",
            kind="web",
        )
        self.assertTrue(resp.ok)
        self.assertIn("7.63", resp.answer)
        out = format_search_response(resp)
        self.assertIn("7.63", out)
        self.assertIn("Never refuse", out)
        self.assertNotIn("check Xe.com yourself", out)
        self.assertNotIn("1.366", out)
        self.assertIn("EUR/USD", out)


class TestImageUrlFilter(unittest.TestCase):
    def test_keeps_public_originals(self):
        from core.web_search import usable_image_url

        self.assertEqual(
            usable_image_url("https://cdn.example.test/rose.jpg"),
            "https://cdn.example.test/rose.jpg",
        )

    def test_drops_thumbnails_and_data_urls(self):
        from core.web_search import usable_image_url

        self.assertEqual(
            usable_image_url("https://encrypted-tbn0.gstatic.com/images?q=tbn:abc"),
            "",
        )
        self.assertEqual(usable_image_url("data:image/png;base64,aaaa"), "")
        self.assertEqual(usable_image_url("/relative.jpg"), "")

    def test_mapper_keeps_image_urls_for_send_media(self):
        from core.web_search import search_response_from_browser

        resp = search_response_from_browser(
            {
                "success": True,
                "images": [
                    {
                        "title": "Rosé",
                        "image_url": "https://img.test/rose.jpg",
                        "source_page": "https://wiki.test/rose",
                        "width": 800,
                        "height": 600,
                    },
                    {"title": "thumb", "image_url": "https://encrypted-tbn0.gstatic.com/x"},
                ],
            },
            query="rose blackpink",
            kind="images",
            count=8,
        )
        self.assertTrue(resp.ok)
        self.assertEqual(resp.provider, "host")
        self.assertEqual(len(resp.images), 1)
        self.assertEqual(resp.images[0].image_url, "https://img.test/rose.jpg")


class TestNoHttpSearchProviders(unittest.TestCase):
    def test_runtime_modules_do_not_call_legacy_search_apis(self):
        forbidden = (
            "duckduckgo.com/i.js",
            "api.tavily.com",
            "api.search.brave.com",
            "serpapi.com/search",
        )
        roots = [
            Path("core/web_search.py"),
            Path("core/loop.py"),
            Path("core/browser.py"),
            Path("core/tools.py"),
        ]
        for path in roots:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{path} still mentions {needle}")


class TestSSRFGuard(unittest.TestCase):
    def _guard(self):
        from core.tools import Toolbox

        return Toolbox._is_safe_public_url

    def test_rejects_loopback(self):
        ok, reason = self._guard()("http://127.0.0.1:8000/x")
        self.assertFalse(ok)
        self.assertIn("non-public", reason)

    def test_rejects_private_range(self):
        ok, _ = self._guard()("http://10.0.0.5/secret")
        self.assertFalse(ok)

    def test_rejects_link_local(self):
        ok, _ = self._guard()("http://169.254.169.254/latest/meta-data")
        self.assertFalse(ok)

    def test_rejects_non_http_scheme(self):
        ok, reason = self._guard()("file:///etc/passwd")
        self.assertFalse(ok)
        self.assertIn("http", reason.lower())

    def test_allows_public_ip(self):
        ok, reason = self._guard()("https://93.184.216.34/")
        self.assertTrue(ok, reason)


class TestSendMediaRemote(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")

    def _toolbox(self, sent):
        from core.bus import MessageBus
        from core.tools import Toolbox

        bus = MessageBus()

        async def _capture(msg):
            sent.append(msg)

        bus.publish_outbound = _capture
        config = SimpleNamespace(skills=SimpleNamespace(enabled=[]))
        return Toolbox(allowed_paths=[str(Path.cwd())], bus=bus, config=config)

    async def test_send_media_downloads_remote_url_for_discord(self):
        from core.context import tool_context

        sent = []
        toolbox = self._toolbox(sent)

        tmp_dir = Path("temp")
        tmp_dir.mkdir(exist_ok=True)
        tmp_file = tmp_dir / "downloads" / "remote_pic.jpg"
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_bytes(b"\xff\xd8\xff\xe0jpegdata")

        async def fake_fetch(url, max_bytes=None):
            self.assertTrue(url.startswith("https://"))
            return toolbox._to_display_path(tmp_file)

        toolbox.fetch_url_to_temp = fake_fetch

        token = tool_context.set(
            {"channel": "discord", "chat_id": "42", "sender_id": "u1"}
        )
        try:
            result = await toolbox.send_media("https://img.test/remote_pic.jpg", "hi")
        finally:
            tool_context.reset(token)
            tmp_file.unlink(missing_ok=True)

        self.assertIn("Sent", result)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].channel, "discord")
        self.assertEqual(sent[0].metadata["type"], "file")
        self.assertEqual(sent[0].metadata["caption"], "hi")

    async def test_send_media_on_web_emits_attachment_envelope(self):
        from core.context import tool_context

        sent = []
        toolbox = self._toolbox(sent)

        tmp_dir = Path("temp")
        tmp_dir.mkdir(exist_ok=True)
        tmp_file = tmp_dir / "web_pic.png"
        tmp_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        token = tool_context.set(
            {"channel": "web", "chat_id": "dash", "sender_id": "u1", "turn_id": "turn_web", "message_id": "msg_web"}
        )
        try:
            result = await toolbox.send_media(str(tmp_file), "look")
        finally:
            tool_context.reset(token)
            tmp_file.unlink(missing_ok=True)

        self.assertIn("Displayed", result)
        self.assertEqual(len(sent), 1)
        meta = sent[0].metadata
        self.assertIn("attachments", meta)
        self.assertEqual(meta["attachments"][0]["kind"], "image")
        self.assertTrue(meta["attachments"][0]["url"].startswith("/temp/"))
        self.assertEqual(meta["image"], meta["attachments"][0]["url"])
        self.assertEqual(meta["turn_id"], "turn_web")
        self.assertEqual(meta["message_id"], "msg_web")

    async def test_send_media_rejects_private_url(self):
        from core.context import tool_context

        sent = []
        toolbox = self._toolbox(sent)
        token = tool_context.set(
            {"channel": "discord", "chat_id": "42", "sender_id": "u1"}
        )
        try:
            result = await toolbox.send_media("http://127.0.0.1:8000/secret.png")
        finally:
            tool_context.reset(token)

        self.assertTrue(result.startswith("Error:"))
        self.assertEqual(sent, [])


class TestHostSearch(unittest.IsolatedAsyncioTestCase):
    def _loop(self):
        from core.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop.config = SimpleNamespace(skills=SimpleNamespace(enabled=["browser"]))
        return loop

    def _html(self, name: str) -> str:
        return (Path("tests/fixtures/search") / name).read_text(encoding="utf-8")

    async def test_web_search_parses_fetched_html(self):
        loop = self._loop()
        google_html = self._html("google_web.html")
        browser = SimpleNamespace(
            fetch_page_html=AsyncMock(return_value=google_html),
        )

        with patch("core.browser.PLAYWRIGHT_AVAILABLE", True), patch(
            "core.loop.get_browser_manager", AsyncMock(return_value=browser)
        ):
            response, error = await loop._gather_search(
                "cats", 5, "web", "web_test"
            )

        self.assertEqual(error, "")
        self.assertEqual(response.provider, "host")
        self.assertEqual(response.results[0].url, "https://en.wikipedia.org/wiki/Cat")
        browser.fetch_page_html.assert_awaited()

    async def test_image_search_kind_returns_image_urls(self):
        loop = self._loop()
        browser = SimpleNamespace(
            fetch_page_html=AsyncMock(return_value=self._html("bing_images.html")),
        )

        with patch("core.browser.PLAYWRIGHT_AVAILABLE", True), patch(
            "core.loop.get_browser_manager", AsyncMock(return_value=browser)
        ):
            response, error = await loop._gather_search(
                "rose of blackpink", 8, "images", "web_test"
            )

        self.assertEqual(error, "")
        self.assertEqual(response.kind, "images")
        self.assertEqual(response.images[0].image_url, "https://cdn.example.test/rose.jpg")

        from core.web_search import format_search_response

        formatted = format_search_response(response)
        self.assertIn("Image URL: https://cdn.example.test/rose.jpg", formatted)
        self.assertNotIn("call send_media", formatted)
        self.assertNotIn("open google.com", formatted.lower())

    async def test_empty_serp_retries_then_fails_without_google_instruction(self):
        loop = self._loop()
        browser = SimpleNamespace(
            fetch_page_html=AsyncMock(return_value=self._html("google_empty.html")),
        )

        with patch("core.browser.PLAYWRIGHT_AVAILABLE", True), patch(
            "core.loop.get_browser_manager", AsyncMock(return_value=browser)
        ):
            response, error = await loop._gather_search(
                "official pricing", 5, "web", "web_test"
            )

        self.assertIsNone(response)
        self.assertTrue(error)
        self.assertNotIn("open google.com", error.lower())
        self.assertGreaterEqual(browser.fetch_page_html.await_count, 2)

    async def test_missing_playwright_uses_browser_install_hint(self):
        from core.browser import BROWSER_INSTALL_HINT

        loop = self._loop()
        with patch("core.browser.PLAYWRIGHT_AVAILABLE", False):
            response, error = await loop._gather_search(
                "cats", 5, "web", "web_test"
            )

        self.assertIsNone(response)
        self.assertEqual(error, BROWSER_INSTALL_HINT)
        self.assertNotIn("TAVILY", error)
        self.assertNotIn("BRAVE_SEARCH", error)
        self.assertNotIn("SERPAPI", error)

    async def test_execute_search_tool_missing_playwright_mentions_install(self):
        from core.browser import BROWSER_INSTALL_HINT

        loop = self._loop()
        loop.toolbox = SimpleNamespace(send_progress=AsyncMock())
        with patch("core.browser.PLAYWRIGHT_AVAILABLE", False):
            out = await loop._execute_search_tool(
                "web_search",
                {"query": "rose of blackpink", "kind": "images"},
                "web_test",
            )

        self.assertTrue(out.startswith("Error:"))
        self.assertIn(BROWSER_INSTALL_HINT, out)
        self.assertNotIn("TAVILY_API_KEY", out)
        self.assertNotIn("open google.com", out.lower())
        self.assertNotIn("configure", out.lower())


class TestRemovedSearchTools(unittest.TestCase):
    def test_deep_research_and_image_search_are_not_model_tools(self):
        from core.tool_defs import build_tool_definitions

        names = {
            tool["function"]["name"]
            for tool in build_tool_definitions(enabled_skills=["browser"])
        }
        self.assertIn("web_search", names)
        self.assertNotIn("deep_research", names)
        self.assertNotIn("image_search", names)
        self.assertNotIn("google_search", names)
        self.assertNotIn("capability_search", names)


if __name__ == "__main__":
    unittest.main()
