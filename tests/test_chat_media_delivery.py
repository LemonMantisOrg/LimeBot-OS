import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROSE_REQUEST = (
    "download a picture of rose of blackpink for me and send me that in this chat"
)


class TestChatMediaIntent(unittest.TestCase):
    def test_rose_request_is_delivery_not_generation(self):
        from core.media_intent import (
            is_chat_media_delivery,
            is_image_generation_request,
        )

        self.assertTrue(is_chat_media_delivery(ROSE_REQUEST))
        self.assertFalse(is_image_generation_request(ROSE_REQUEST))

    def test_generate_request_is_not_delivery(self):
        from core.media_intent import (
            is_chat_media_delivery,
            is_image_generation_request,
        )

        prompt = "generate an image of a lime robot"
        self.assertTrue(is_image_generation_request(prompt))
        self.assertFalse(is_chat_media_delivery(prompt))


class TestChatMediaPromptAndTools(unittest.TestCase):
    def test_stable_prompt_puts_media_delivery_above_generation(self):
        from core.media_intent import MEDIA_DELIVERY_RULES
        from core.prompt import build_stable_system_prompt

        prompt = build_stable_system_prompt(
            sender_id="owner",
            channel="web",
            chat_id="chat",
            model="test-model",
            allowed_paths=[],
            skill_registry=None,
            config=SimpleNamespace(
                llm=SimpleNamespace(enable_dynamic_personality=False),
                personality_whitelist=["owner"],
            ),
            soul=(
                "Core values matter. Truth, boundaries, and personality are important. "
                "This soul description is long enough to pass validation and explain who I am."
            ),
            identity_raw=(
                "# IDENTITY.md - Who I Am\n\n"
                "*   **Name:** LimeBot\n"
                "*   **Emoji:** 🍋\n"
                "*   **Style:** Clear and direct\n"
            ),
            sender_name="Owner",
        )

        self.assertIn(MEDIA_DELIVERY_RULES.strip().split("\n")[0], prompt)
        self.assertIn("web_search", prompt)
        self.assertIn('kind="images"', prompt)
        self.assertIn("Do not call `send_media`", prompt)
        self.assertLess(prompt.find("MEDIA DELIVERY"), prompt.find("IMAGE GENERATION"))
        self.assertIn("profile picture", prompt)
        self.assertIn("Do not read or dump AGENTS.md", prompt)
        self.assertNotIn("Core Module Reference", prompt)
        self.assertNotIn("DurableJobQueue", prompt)
        self.assertNotIn("If the user gives you a direct avatar/profile image URL, do NOT browse, search, or download it.", prompt)

    def test_tool_docs_route_existing_photos_to_search_and_send(self):
        from core.tool_defs import build_tool_definitions

        tools = build_tool_definitions(enabled_skills=[])
        by_name = {tool["function"]["name"]: tool["function"]["description"] for tool in tools}

        self.assertIn("web_search", by_name)
        self.assertNotIn("image_search", by_name)
        self.assertNotIn("google_search", by_name)
        self.assertNotIn("capability_search", by_name)
        self.assertNotIn("deep_research", by_name)
        self.assertIn("images", by_name["web_search"])
        self.assertIn("Do NOT use this to download", by_name["generate_image"])
        self.assertIn("photo into the current chat", by_name["spawn_agent"])

    def test_shortlist_maps_send_picture_to_web_search_not_generate(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        selected = shortlist_tool_definitions(tools, ROSE_REQUEST, channel="web")
        names = {tool["function"]["name"] for tool in selected}

        self.assertEqual(names, {"web_search"})
        self.assertNotIn("send_media", names)
        self.assertNotIn("google_search", names)
        self.assertNotIn("capability_search", names)
        self.assertNotIn("browser_click", names)
        self.assertNotIn("generate_image", names)
        self.assertNotIn("spawn_agent", names)
        self.assertNotIn("run_command", names)

    def test_search_tools_are_registered_without_browser_or_keys(self):
        from core.tool_defs import build_tool_definitions

        names = {
            tool["function"]["name"]
            for tool in build_tool_definitions(enabled_skills=[], search_available=False)
        }
        self.assertNotIn("image_search", names)
        self.assertIn("web_search", names)
        kinds = next(
            tool["function"]["parameters"]["properties"]["kind"]["enum"]
            for tool in build_tool_definitions(enabled_skills=[])
            if tool["function"]["name"] == "web_search"
        )
        self.assertEqual(kinds, ["web", "news", "images"])


class TestChatMediaPromptBloat(unittest.IsolatedAsyncioTestCase):
    async def test_live_prompt_skips_skill_subagent_and_agents_dump(self):
        from core.loop import AgentLoop
        from core.media_intent import MEDIA_DELIVERY_RULES
        from unittest.mock import AsyncMock

        agent = object.__new__(AgentLoop)
        agent._get_stable_prompt = AsyncMock(return_value="STABLE\n" + MEDIA_DELIVERY_RULES)
        agent.config = SimpleNamespace(
            llm=SimpleNamespace(enable_dynamic_personality=False),
            personality_whitelist=["owner"],
        )
        agent._get_capability_turn_context = lambda *_args, **_kwargs: {
            "routing_text": ROSE_REQUEST
        }
        agent.skill_registry = SimpleNamespace(
            get_forced_prompt_addition=lambda _name: "SHOULD_NOT_INJECT_SKILL",
            get_relevant_prompt_additions=lambda _text: "SHOULD_NOT_INJECT_SKILL",
        )
        agent.subagent_registry = SimpleNamespace(
            get_prompt_additions=lambda _text: "SHOULD_NOT_INJECT_SUBAGENT"
        )
        agent._capability_catalog_prompt = lambda: "SHOULD_NOT_INJECT_CAPABILITY"

        with patch(
            "core.loop.prompt_module.is_setup_complete",
            return_value=True,
        ), patch(
            "core.loop.prompt_module.should_load_private_context",
            return_value=False,
        ), patch(
            "core.loop.prompt_module.get_volatile_prompt_suffix",
            return_value="\n--- CONTEXT & MEMORY ---\n",
        ):
            prompt = await agent._build_full_system_prompt(
                sender_id="owner",
                channel="web",
                chat_id="chat",
                current_message=ROSE_REQUEST,
            )

        self.assertIn("MEDIA DELIVERY", prompt)
        self.assertNotIn("SHOULD_NOT_INJECT_SKILL", prompt)
        self.assertNotIn("SHOULD_NOT_INJECT_SUBAGENT", prompt)
        self.assertNotIn("SHOULD_NOT_INJECT_CAPABILITY", prompt)
        self.assertNotIn("Core Module Reference", prompt)


class TestSendMediaWebEnvelope(unittest.IsolatedAsyncioTestCase):
    async def test_web_send_media_stamps_turn_ids_and_image_envelope(self):
        from pathlib import Path
        from types import SimpleNamespace

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

        tmp_dir = Path("temp")
        tmp_dir.mkdir(exist_ok=True)
        tmp_file = tmp_dir / "rose_envelope.png"
        tmp_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        token = tool_context.set(
            {
                "channel": "web",
                "chat_id": "dash",
                "sender_id": "u1",
                "turn_id": "turn_media",
                "message_id": "msg_media",
            }
        )
        try:
            result = await toolbox.send_media(str(tmp_file), "Rosé")
        finally:
            tool_context.reset(token)
            tmp_file.unlink(missing_ok=True)

        self.assertIn("Displayed", result)
        self.assertEqual(len(sent), 1)
        meta = sent[0].metadata
        self.assertEqual(meta["turn_id"], "turn_media")
        self.assertEqual(meta["message_id"], "msg_media")
        self.assertEqual(meta["image"], meta["attachments"][0]["url"])
        self.assertEqual(meta["attachments"][0]["kind"], "image")
        self.assertTrue(meta["attachments"][0]["url"].startswith("/temp/"))
        self.assertEqual(sent[0].content, "Rosé")


class TestHostOwnedPhotoAttach(unittest.IsolatedAsyncioTestCase):
    async def test_host_attach_stamps_turn_ids_and_image_envelope(self):
        from pathlib import Path
        from types import SimpleNamespace

        from core.bus import MessageBus
        from core.context import tool_context
        from core.loop import AgentLoop
        from core.tools import Toolbox
        from core.web_search import ImageResult, SearchResponse

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
        loop = AgentLoop.__new__(AgentLoop)
        loop.toolbox = toolbox

        tmp_dir = Path("temp")
        tmp_dir.mkdir(exist_ok=True)
        tmp_file = tmp_dir / "host_rose.png"
        tmp_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        async def fake_fetch(url, max_bytes=None):
            self.assertEqual(url, "https://cdn.example.test/rose.jpg")
            return str(tmp_file)

        toolbox.fetch_url_to_temp = fake_fetch
        response = SearchResponse(kind="images", query="rose blackpink", provider="host")
        response.images = [
            ImageResult(
                title="Rosé",
                image_url="https://cdn.example.test/rose.jpg",
                source_page="https://wiki.test/rose",
            )
        ]
        token = tool_context.set(
            {
                "channel": "web",
                "chat_id": "dash",
                "sender_id": "u1",
                "turn_id": "turn_host",
                "message_id": "msg_host",
                "user_text": ROSE_REQUEST,
            }
        )
        try:
            await loop._maybe_host_attach_search_image(response, "rose blackpink")
        finally:
            tool_context.reset(token)
            tmp_file.unlink(missing_ok=True)

        self.assertTrue(response.attached)
        self.assertEqual(len(sent), 1)
        meta = sent[0].metadata
        self.assertEqual(meta["turn_id"], "turn_host")
        self.assertEqual(meta["message_id"], "msg_host")
        self.assertEqual(meta["image"], meta["attachments"][0]["url"])
        self.assertEqual(meta["attachments"][0]["kind"], "image")
        self.assertTrue(meta["attachments"][0]["url"].startswith("/temp/"))

    def test_generate_image_turn_is_exclusive(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        selected = shortlist_tool_definitions(
            tools, "generate an image of a lime robot", channel="web"
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertEqual(names, {"generate_image"})

    def test_discord_photo_send_keeps_send_media(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        selected = shortlist_tool_definitions(tools, ROSE_REQUEST, channel="discord")
        names = {tool["function"]["name"] for tool in selected}
        self.assertEqual(names, {"web_search", "send_media"})

    def test_browser_surface_is_three_tools(self):
        from core.tool_defs import build_tool_definitions

        names = {
            tool["function"]["name"]
            for tool in build_tool_definitions(enabled_skills=["browser"])
        }
        browser_names = {name for name in names if name.startswith("browser_")}
        self.assertEqual(
            browser_names,
            {"browser_navigate", "browser_act", "browser_extract"},
        )

    def test_exclusive_shortlist_applies_even_when_global_shortlist_is_off(self):
        from core.loop import AgentLoop
        from core.tool_defs import build_tool_definitions

        all_tools = build_tool_definitions(enabled_skills=["browser"])
        agent = object.__new__(AgentLoop)
        agent.config = SimpleNamespace(tool_shortlist_enabled=False)
        agent.skill_registry = SimpleNamespace(get_required_tool_names=lambda _name: [])
        agent._get_tool_definitions = lambda: all_tools
        agent._log_tool_debug = lambda *args, **kwargs: None

        selected = agent._get_tool_definitions_for_turn(
            ROSE_REQUEST, session_key="web:dash"
        )
        names = {tool["function"]["name"] for tool in selected}
        self.assertEqual(names, {"web_search"})
        self.assertNotIn("send_media", names)
        self.assertNotIn("browser_click", names)
        self.assertNotIn("google_search", names)
        self.assertNotIn("capability_search", names)


if __name__ == "__main__":
    unittest.main()
