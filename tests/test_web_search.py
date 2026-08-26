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

        resp = SearchResponse(kind="web", query="cats", provider="browser")
        resp.answer = "Cats are mammals."
        resp.results = [SearchResult(title="Cats", url="https://ex.test/cats", snippet="Feline")]
        out = format_search_response(resp)
        self.assertIn("https://ex.test/cats", out)
        self.assertIn("Direct answer", out)
        self.assertIn("(via browser)", out)

    def test_image_results_include_send_media_hint(self):
        from core.web_search import (
            SearchResponse,
            ImageResult,
            format_search_response,
        )

        resp = SearchResponse(kind="images", query="pup", provider="browser")
        resp.images = [
            ImageResult(title="Puppy", image_url="https://img.test/a.jpg", source_page="https://p.test")
        ]
        out = format_search_response(resp)
        self.assertIn("https://img.test/a.jpg", out)
        self.assertIn("Image URL:", out)
        self.assertIn("send_media", out)


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
        self.assertEqual(resp.provider, "browser")
        self.assertEqual(len(resp.images), 1)
        self.assertEqual(resp.images[0].image_url, "https://img.test/rose.jpg")


class TestNoHttpSearchProviders(unittest.TestCase):
    def test_runtime_modules_do_not_call_legacy_search_apis(self):
        forbidden = (
            "duckduckgo.com/i.js",
            "html.duckduckgo.com",
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


class TestBrowserSearch(unittest.IsolatedAsyncioTestCase):
    def _loop(self):
        from core.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop.config = SimpleNamespace(skills=SimpleNamespace(enabled=["browser"]))
        return loop

    async def test_web_search_uses_mocked_google_search(self):
        loop = self._loop()
        browser = SimpleNamespace(
            google_search=AsyncMock(
                return_value={
                    "success": True,
                    "results": [
                        {
                            "title": "Official pricing",
                            "url": "https://example.test/pricing",
                            "snippet": "Current price",
                        }
                    ],
                }
            ),
            image_search=AsyncMock(return_value={"success": False, "images": []}),
        )

        with patch("core.browser.PLAYWRIGHT_AVAILABLE", True), patch(
            "core.loop.get_browser_manager", AsyncMock(return_value=browser)
        ):
            response, error = await loop._gather_search(
                "official pricing", 5, "web", "web_test"
            )

        self.assertEqual(error, "")
        self.assertEqual(response.provider, "browser")
        self.assertEqual(response.results[0].url, "https://example.test/pricing")
        browser.google_search.assert_awaited()
        browser.image_search.assert_not_called()

    async def test_image_search_returns_image_urls(self):
        loop = self._loop()
        browser = SimpleNamespace(
            google_search=AsyncMock(),
            image_search=AsyncMock(
                return_value={
                    "success": True,
                    "images": [
                        {
                            "title": "Rosé BLACKPINK",
                            "image_url": "https://img.test/rose.jpg",
                            "source_page": "https://wiki.test/rose",
                        }
                    ],
                }
            ),
        )

        with patch("core.browser.PLAYWRIGHT_AVAILABLE", True), patch(
            "core.loop.get_browser_manager", AsyncMock(return_value=browser)
        ):
            response, error = await loop._gather_search(
                "rose of blackpink", 8, "images", "web_test"
            )

        self.assertEqual(error, "")
        self.assertEqual(response.kind, "images")
        self.assertEqual(response.images[0].image_url, "https://img.test/rose.jpg")
        browser.image_search.assert_awaited()
        browser.google_search.assert_not_called()

        from core.web_search import format_search_response

        formatted = format_search_response(response)
        self.assertIn("Image URL: https://img.test/rose.jpg", formatted)
        self.assertIn("send_media", formatted)

    async def test_unparseable_search_is_a_failure(self):
        loop = self._loop()
        browser = SimpleNamespace(
            google_search=AsyncMock(
                return_value={
                    "success": False,
                    "results": [],
                    "error": "Google results were present but could not be parsed.",
                }
            )
        )

        with patch("core.browser.PLAYWRIGHT_AVAILABLE", True), patch(
            "core.loop.get_browser_manager", AsyncMock(return_value=browser)
        ):
            response, error = await loop._gather_search(
                "official pricing", 5, "web", "web_test"
            )

        self.assertIsNone(response)
        self.assertIn("could not be parsed", error)

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
                "image_search", {"query": "rose of blackpink"}, "web_test"
            )

        self.assertTrue(out.startswith("Error:"))
        self.assertIn(BROWSER_INSTALL_HINT, out)
        self.assertNotIn("TAVILY_API_KEY", out)
        self.assertNotIn("configure", out.lower())


class TestDeepResearch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")

    async def test_deep_research_synthesizes_with_citations(self):
        from core.loop import AgentLoop
        from core.web_search import SearchResponse, SearchResult

        loop = AgentLoop.__new__(AgentLoop)

        resp = SearchResponse(kind="web", query="q", provider="browser")
        resp.results = [
            SearchResult(title="A", url="https://a.test", snippet="s", content="Cats. " * 100),
            SearchResult(title="B", url="https://b.test", snippet="s2", content="Dogs. " * 100),
        ]

        async def fake_gather(query, count, kind, session_key, on_progress=None):
            return resp, ""

        loop._gather_search = fake_gather

        class _TB:
            async def send_progress(self, *a, **k):
                return None

            async def fetch_readable_text(self, url, max_chars=4000):
                return "readable content"

        loop.toolbox = _TB()

        class _Msg:
            content = "Cats [1] and dogs [2] coexist."

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        class _LLM:
            def resolve_provider(self, model, default_base_url=None):
                return object()

            async def complete(self, provider, req):
                return _Resp()

        loop.llm_client = _LLM()
        loop.model = "test-model"
        loop.config = SimpleNamespace(llm=SimpleNamespace(base_url=None))

        out = await loop._run_deep_research("q", {}, "sess::web")

        self.assertIn("[1]", out)
        self.assertIn("**Sources:**", out)
        self.assertIn("https://a.test", out)
        self.assertIn("https://b.test", out)


if __name__ == "__main__":
    unittest.main()
