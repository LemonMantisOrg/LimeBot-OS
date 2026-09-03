import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.browser import reset_playwright_launch_dead


IG_REQUEST = (
    "https://www.instagram.com/p/DcxM0EIG6UD/?img_index=3 ... "
    "bajame esas fotos y mandamelas"
)
IG_URL = "https://www.instagram.com/p/DcxM0EIG6UD/?img_index=3"


def _escaped_carousel_html(shortcode="DcxM0EIG6UD"):
    """Nested/escaped sidecar JSON like Instagram's public embed page."""
    edges = []
    for index in range(6):
        edges.append(
            {
                "node": {
                    "id": str(1000 + index),
                    "shortcode": f"child{index}",
                    "is_video": index >= 4,
                    "display_url": (
                        f"https://scontent.cdninstagram.com/v/t51.82787-15/"
                        f"slide{index}.jpg"
                    ),
                }
            }
        )
    inner = json.dumps({"edges": edges}, separators=(",", ":"))
    wrapped = '{"shortcode_media":{"edge_sidecar_to_children":' + inner + "}}"
    escaped = wrapped.replace("\\", "\\\\").replace('"', '\\"').replace("/", "\\/")
    return (
        "<html><head></head><body><script>"
        'window.__additionalDataLoaded("/p/' + shortcode + '/embed/captioned/", "'
        + escaped
        + '");</script>'
        '<img src="https://scontent.cdninstagram.com/v/t51.82787-19/profile.jpg">'
        '<meta property="og:image" content="https://scontent.cdninstagram.com/v/t51.82787-15/og_video_thumb.jpg">'
        "</body></html>"
    )


class TestInstagramSidecarParse(unittest.TestCase):
    def test_shortcode_dcxm0eig6ud_yields_six_nodes_four_stills(self):
        from core.instagram import parse_sidecar_nodes

        nodes = parse_sidecar_nodes(_escaped_carousel_html())
        self.assertEqual(len(nodes), 6)
        self.assertEqual(sum(1 for node in nodes if not node.is_video), 4)
        self.assertEqual(sum(1 for node in nodes if node.is_still), 4)
        self.assertEqual(sum(1 for node in nodes if node.is_video), 2)
        self.assertTrue(all("t51.82787-15" in node.display_url for node in nodes))
        self.assertFalse(any(node.is_profile_pic for node in nodes))

    def test_parser_skips_profile_pics_and_ignores_og_image(self):
        from core.instagram import parse_sidecar_nodes

        html = (
            '<html><meta property="og:image" content="https://cdn.test/og.jpg">'
            '<img src="https://scontent.cdninstagram.com/v/t51.82787-19/profile.jpg">'
            "</html>"
        )
        self.assertEqual(parse_sidecar_nodes(html), [])


class TestInstagramToolSurface(unittest.TestCase):
    def test_bajame_turn_hides_browser_navigate(self):
        from core.media_intent import exclusive_tools_for_turn, is_instagram_photo_send
        from core.tool_capability import hidden_tools_for_instagram_photo_send
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        self.assertTrue(is_instagram_photo_send(IG_REQUEST))
        self.assertEqual(exclusive_tools_for_turn(IG_REQUEST, "discord"), {"send_media"})
        hidden = hidden_tools_for_instagram_photo_send(IG_REQUEST)
        self.assertIn("browser_navigate", hidden)
        self.assertIn("browser_act", hidden)
        self.assertIn("run_command", hidden)

        selected = shortlist_tool_definitions(
            build_tool_definitions(enabled_skills=["browser"]),
            IG_REQUEST,
            channel="discord",
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertEqual(names, {"send_media"})
        self.assertNotIn("browser_navigate", names)
        self.assertNotIn("web_search", names)

    def test_full_schema_path_still_hides_instagram_browser(self):
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
            IG_REQUEST, session_key="discord:ig"
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertEqual(names, {"send_media"})
        self.assertNotIn("browser_navigate", names)


class TestInstagramHostRefuseAndSend(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import loguru  # noqa: F401
        except Exception:
            raise unittest.SkipTest("Missing dependencies (loguru).")
        reset_playwright_launch_dead()

    async def asyncTearDown(self):
        reset_playwright_launch_dead()

    def _agent(self):
        from core.bus import MessageBus
        from core.loop import AgentLoop

        class _TestAgentLoop(AgentLoop):
            async def _init_skills_and_tools(self) -> None:
                self._tool_definitions = []
                self._warmed = True

        return _TestAgentLoop(bus=MessageBus())

    async def test_browser_navigate_instagram_post_is_refused(self):
        from core.context import tool_context

        agent = self._agent()
        launched = []

        async def _boom(*args, **kwargs):
            launched.append(True)
            raise AssertionError("Playwright must not launch for an IG photo-send")

        token = tool_context.set({"user_text": IG_REQUEST, "attachments": []})
        try:
            with patch("core.loop.get_browser_manager", _boom):
                result = await agent._execute_tool(
                    "browser_navigate",
                    {"url": IG_URL},
                    session_key="discord:ig",
                )
        finally:
            tool_context.reset(token)

        self.assertEqual(launched, [])
        self.assertTrue(str(result).startswith("Error:"))
        self.assertIn("Instagram", str(result))
        self.assertNotIn("--no-sandbox", str(result))
        self.assertNotIn("launch_persistent_context", str(result))

    async def test_genuine_page_navigate_still_reaches_browser_tool(self):
        agent = self._agent()
        called = []

        async def _fake_browser(function_name, args, session_key):
            called.append((function_name, dict(args), session_key))
            return "opened https://example.com/downloads/"

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

    async def test_playwright_launch_failure_is_one_line_without_chrome_flags(self):
        from core.browser import BROWSER_LAUNCH_FAILED_ERROR, mark_playwright_launch_dead

        agent = self._agent()
        launches = []

        async def _boom(*args, **kwargs):
            launches.append(True)
            raise RuntimeError(
                "Failed to start browser in both persistent and ephemeral modes. "
                "Persistent error: BrowserType.launch_persistent_context: "
                "Target page, context or browser has been closed | "
                "Fallback error: args=['--no-sandbox']"
            )

        with patch("core.loop.get_browser_manager", _boom):
            first = await agent._execute_browser_tool(
                "browser_navigate",
                {"url": "https://example.com/"},
                "web:page",
            )
            second = await agent._execute_browser_tool(
                "browser_navigate",
                {"url": "https://example.com/"},
                "web:page",
            )

        self.assertEqual(first, BROWSER_LAUNCH_FAILED_ERROR)
        self.assertEqual(second, BROWSER_LAUNCH_FAILED_ERROR)
        self.assertEqual(len(launches), 1)
        self.assertNotIn("--no-sandbox", first)
        self.assertNotIn("launch_persistent_context", first)
        mark_playwright_launch_dead()

    async def test_local_stills_are_sent_even_if_playwright_dead(self):
        from core.browser import mark_playwright_launch_dead
        from core.bus import MessageBus
        from core.context import tool_context
        from core.instagram import InstagramCarousel, SidecarNode
        from core.loop import AgentLoop
        from core.tools import Toolbox

        mark_playwright_launch_dead()
        sent = []
        bus = MessageBus()

        async def _capture(msg):
            sent.append(msg)

        bus.publish_outbound = _capture
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())],
            bus=bus,
            config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
        )
        tmp_dir = Path("temp") / "instagram" / "DcxM0EIG6UD"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        photos = []
        for index in range(4):
            path = tmp_dir / f"slide_{index}.jpg"
            path.write_bytes(b"\xff\xd8\xff\xe0jpeg" + bytes([index]))
            photos.append(str(path))

        carousel = InstagramCarousel(
            shortcode="DcxM0EIG6UD",
            nodes=[
                SidecarNode(
                    display_url=f"https://scontent.cdninstagram.com/v/t51.82787-15/s{i}.jpg",
                    is_video=i >= 4,
                    shortcode=f"c{i}",
                )
                for i in range(6)
            ],
            photo_paths=photos,
        )

        async def _fake_fetch(shortcode):
            self.assertEqual(shortcode, "DcxM0EIG6UD")
            return carousel

        toolbox._fetch_instagram_carousel = _fake_fetch
        loop = AgentLoop.__new__(AgentLoop)
        loop.toolbox = toolbox
        loop._turn_attachments = {}
        msg = SimpleNamespace(channel="discord", chat_id="42", sender_id="u1", content=IG_REQUEST)

        launched = []

        async def _no_browser(*args, **kwargs):
            launched.append(True)
            raise AssertionError("Playwright must not run after stills exist")

        token = tool_context.set(
            {
                "channel": "discord",
                "chat_id": "42",
                "sender_id": "u1",
                "turn_id": "turn_ig",
                "message_id": "msg_ig",
                "user_text": IG_REQUEST,
            }
        )
        try:
            with patch("core.loop.get_browser_manager", _no_browser):
                await loop._maybe_host_deliver_instagram_photos(
                    msg, IG_REQUEST, turn_id="turn_ig", message_id="msg_ig"
                )
        finally:
            tool_context.reset(token)
            for path in photos:
                Path(path).unlink(missing_ok=True)

        self.assertEqual(launched, [])
        self.assertEqual(len(sent), 4)
        self.assertTrue(all(item.channel == "discord" for item in sent))
        self.assertTrue(all(item.metadata.get("type") == "file" for item in sent))
        self.assertTrue(toolbox.media_delivered_this_turn("turn_ig"))
        self.assertTrue(loop._turn_already_delivered_media("turn_ig"))
        self.assertFalse(any("sandbox:" in (item.content or "") for item in sent))
        self.assertFalse(any("http" in str(item.metadata.get("file_path")) for item in sent))

    async def test_send_media_of_local_jpeg_still_works(self):
        from core.bus import MessageBus
        from core.context import tool_context
        from core.tools import Toolbox

        sent = []
        bus = MessageBus()

        async def _capture(msg):
            sent.append(msg)

        bus.publish_outbound = _capture
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())],
            bus=bus,
            config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
        )
        tmp = Path("temp")
        tmp.mkdir(exist_ok=True)
        jpeg = tmp / "already_attached.jpg"
        jpeg.write_bytes(b"\xff\xd8\xff\xe0attached")
        token = tool_context.set(
            {
                "channel": "discord",
                "chat_id": "99",
                "sender_id": "u1",
                "turn_id": "turn_attached",
            }
        )
        try:
            result = await toolbox.send_media(str(jpeg), "here")
        finally:
            tool_context.reset(token)
            jpeg.unlink(missing_ok=True)

        self.assertIn("Sent", result)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].metadata["type"], "file")


if __name__ == "__main__":
    unittest.main()
