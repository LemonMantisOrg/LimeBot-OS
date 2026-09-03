import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


JENNIE_CDN = (
    "https://cdn.discordapp.com/attachments/111/222/jennie.jpg"
    "?ex=66f0&is=66ef&hm=deadbeef"
)
JENNIE_PATH = "temp/discord_uploads/chat-1/1710000000_0_jennie.jpg"


def _image_attachment(**overrides):
    payload = {
        "kind": "image",
        "name": "jennie.jpg",
        "mime_type": "image/jpeg",
        "path": JENNIE_PATH,
        "url": JENNIE_CDN,
    }
    payload.update(overrides)
    return payload


class TestImageLocatorDetection(unittest.TestCase):
    def test_image_url_ignores_discord_query_string(self):
        from core.tool_capability import is_image_locator, is_image_navigate_target

        self.assertTrue(is_image_locator(JENNIE_CDN))
        self.assertTrue(is_image_navigate_target(JENNIE_CDN))
        self.assertFalse(is_image_locator("https://www.python.org/downloads/"))
        self.assertFalse(is_image_navigate_target("https://example.com/page"))

    def test_image_path_and_content_type(self):
        from core.tool_capability import is_image_locator, is_image_read_target

        self.assertTrue(is_image_locator(JENNIE_PATH))
        self.assertTrue(is_image_locator("photo", content_type="image/png"))
        self.assertTrue(is_image_read_target(JENNIE_PATH))
        self.assertFalse(is_image_read_target("core/loop.py"))
        self.assertFalse(is_image_locator("https://example.com/report.pdf"))


class TestHostRefuseBeforeExecute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")

    def _agent(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

        return _TestAgentLoop(bus=MessageBus())

    async def test_read_file_on_jpeg_is_refused_before_toolbox(self):
        agent = self._agent()
        tmp = Path("temp")
        tmp.mkdir(exist_ok=True)
        jpeg = tmp / "jennie_refuse.jpg"
        jpeg.write_bytes(b"\xff\xd8\xff\xe0JFIF" + b"\x00" * 32)
        called = []

        async def _spy_read_file(*args, **kwargs):
            called.append((args, kwargs))
            return "should-not-dump-jfif"

        agent.toolbox.read_file = _spy_read_file
        try:
            result = await agent._execute_tool(
                "read_file",
                {"path": str(jpeg), "max_chars": 2000},
                session_key="discord:jennie",
            )
        finally:
            jpeg.unlink(missing_ok=True)

        self.assertEqual(called, [])
        self.assertTrue(str(result).startswith("Error:"))
        self.assertIn("read_file cannot dump image bytes", str(result))
        self.assertNotIn("JFIF", str(result))

    async def test_read_file_on_png_gif_webp_is_refused(self):
        agent = self._agent()
        agent.toolbox.read_file = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("read_file must not run")
        )
        for name in ("shot.png", "loop.gif", "card.webp"):
            result = await agent._execute_tool(
                "read_file", {"path": f"temp/{name}"}, session_key="web:img"
            )
            self.assertTrue(str(result).startswith("Error:"), name)
            self.assertIn("image", str(result).lower(), name)

    async def test_read_file_on_discord_upload_path_is_refused(self):
        agent = self._agent()
        agent._turn_attachments["discord:jennie"] = [_image_attachment()]
        called = []

        async def _spy_read_file(*args, **kwargs):
            called.append(True)
            return "nope"

        agent.toolbox.read_file = _spy_read_file
        result = await agent._execute_tool(
            "read_file",
            {"path": JENNIE_PATH, "max_chars": 2000},
            session_key="discord:jennie",
        )
        self.assertEqual(called, [])
        self.assertTrue(str(result).startswith("Error:"))
        self.assertIn("vision context", str(result))

    async def test_browser_navigate_image_url_is_refused_before_playwright(self):
        agent = self._agent()
        launched = []

        async def _boom(*args, **kwargs):
            launched.append(True)
            raise AssertionError("Playwright must not launch for a jpg URL")

        with patch("core.loop.get_browser_manager", _boom):
            result = await agent._execute_tool(
                "browser_navigate",
                {"url": JENNIE_CDN},
                session_key="discord:jennie",
            )

        self.assertEqual(launched, [])
        self.assertTrue(str(result).startswith("Error:"))
        self.assertIn("browser_navigate cannot open image URLs", str(result))

    async def test_genuine_text_file_read_still_executes(self):
        agent = self._agent()
        tmp = Path("temp")
        tmp.mkdir(exist_ok=True)
        text = tmp / "genuine_read.txt"
        text.write_text("hello genuine read", encoding="utf-8")
        try:
            result = await agent._execute_tool(
                "read_file", {"path": str(text)}, session_key="web:fs"
            )
        finally:
            text.unlink(missing_ok=True)
        self.assertIn("hello genuine read", str(result))

    async def test_genuine_page_navigate_still_reaches_browser_tool(self):
        agent = self._agent()
        called = []

        async def _fake_browser(function_name, args, session_key):
            called.append((function_name, dict(args), session_key))
            return "opened https://example.com"

        agent._execute_browser_tool = _fake_browser
        result = await agent._execute_tool(
            "browser_navigate",
            {"url": "https://example.com/downloads/"},
            session_key="web:page",
        )
        self.assertEqual(
            called,
            [
                (
                    "browser_navigate",
                    {"url": "https://example.com/downloads/"},
                    "web:page",
                )
            ],
        )
        self.assertIn("example.com", str(result))


class TestImageAttachmentToolSurface(unittest.TestCase):
    def test_image_attachment_turn_hides_see_image_tools(self):
        from core.tool_capability import hidden_tools_for_image_attachments
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        attachments = [_image_attachment()]
        hidden = hidden_tools_for_image_attachments(
            "who is this in the photos", attachments
        )
        self.assertEqual(hidden, {"read_file", "browser_navigate"})

        tools = build_tool_definitions(enabled_skills=[])
        selected = shortlist_tool_definitions(
            tools,
            "who is this in the photos",
            channel="discord",
            attachments=attachments,
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertNotIn("read_file", names)
        self.assertNotIn("browser_navigate", names)

    def test_image_attachment_plus_real_page_keeps_navigate(self):
        from core.tool_capability import hidden_tools_for_image_attachments
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        attachments = [_image_attachment()]
        hidden = hidden_tools_for_image_attachments(
            "also open https://www.python.org/downloads/", attachments
        )
        self.assertNotIn("browser_navigate", hidden)

        selected = shortlist_tool_definitions(
            build_tool_definitions(enabled_skills=[]),
            "also open https://www.python.org/downloads/",
            channel="discord",
            attachments=attachments,
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertIn("browser_navigate", names)

    def test_image_attachment_plus_text_file_keeps_read_file(self):
        from core.tool_capability import hidden_tools_for_image_attachments
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        attachments = [_image_attachment()]
        hidden = hidden_tools_for_image_attachments(
            "read README.md after you look at the photos", attachments
        )
        self.assertNotIn("read_file", hidden)

        selected = shortlist_tool_definitions(
            build_tool_definitions(enabled_skills=[]),
            "read README.md after you look at the photos",
            channel="discord",
            attachments=attachments,
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertIn("read_file", names)

    def test_full_schema_path_still_hides_see_image_tools(self):
        from core.loop import AgentLoop
        from core.tool_defs import build_tool_definitions

        agent = object.__new__(AgentLoop)
        agent.config = SimpleNamespace(tool_shortlist_enabled=False)
        agent.skill_registry = None
        agent._session_capability_state = {}
        agent._turn_attachments = {}
        agent._get_tool_definitions = lambda: build_tool_definitions(enabled_skills=[])
        agent._log_tool_debug = lambda *args, **kwargs: None

        selected = agent._get_tool_definitions_for_turn(
            "what do you see",
            session_key="discord:jennie",
            attachments=[_image_attachment()],
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertNotIn("read_file", names)
        self.assertNotIn("browser_navigate", names)
        self.assertIn("web_search", names)

    def test_image_only_url_is_not_a_named_page(self):
        from core.tool_defs import user_named_a_page

        self.assertFalse(user_named_a_page(JENNIE_CDN))
        self.assertFalse(
            user_named_a_page("look at these\n" + JENNIE_CDN)
        )
        self.assertTrue(
            user_named_a_page("open https://www.python.org/downloads/")
        )

    def test_image_only_url_does_not_require_initial_tool_call(self):
        from core.loop import AgentLoop

        tools = [{"function": {"name": "browser_navigate"}}]
        self.assertFalse(
            AgentLoop._requires_initial_tool_call(
                f"look at {JENNIE_CDN}", tools
            )
        )
        self.assertTrue(
            AgentLoop._requires_initial_tool_call(
                "Analice https://example.com y haga un resumen", tools
            )
        )


class TestAttachmentSummaryDoesNotOfferImagePaths(unittest.TestCase):
    def test_image_summary_omits_discord_upload_path(self):
        from core.loop import AgentLoop

        summary = AgentLoop._build_attachment_summary(
            [
                _image_attachment(),
                {
                    "kind": "document",
                    "name": "report.pdf",
                    "mime_type": "application/pdf",
                    "path": "temp/discord_uploads/chat-1/report.pdf",
                    "extracted_text": "Quarterly metrics",
                },
            ]
        )
        self.assertIn("1 image(s), 1 document(s)", summary)
        self.assertIn("Attached image 1: jennie.jpg", summary)
        self.assertIn("vision context", summary)
        self.assertNotIn(JENNIE_PATH, summary)
        self.assertNotIn("Saved as `temp/discord_uploads/chat-1/1710000000_0_jennie.jpg`", summary)
        self.assertIn("Saved as `temp/discord_uploads/chat-1/report.pdf`", summary)
        self.assertIn("Text was extracted", summary)


class TestDiscordDoesNotDumpImageUrls(unittest.TestCase):
    def test_extra_image_urls_are_not_offered_as_page_text(self):
        from channels.discord import DiscordChannel

        visible = DiscordChannel._user_visible_attachment_urls(
            [
                "https://cdn.example.com/document.pdf",
                JENNIE_CDN,
                "https://cdn.discordapp.com/attachments/1/2/second.png",
            ]
        )
        self.assertEqual(visible, ["https://cdn.example.com/document.pdf"])
