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
        self.assertIn("image_search", prompt)
        self.assertIn("send_media", prompt)
        self.assertLess(prompt.find("MEDIA DELIVERY"), prompt.find("IMAGE GENERATION"))
        self.assertIn("identity-only rule does NOT apply", prompt)
        self.assertIn("Do not read or dump AGENTS.md", prompt)
        self.assertNotIn("Core Module Reference", prompt)
        self.assertNotIn("DurableJobQueue", prompt)
        self.assertNotIn("If the user gives you a direct avatar/profile image URL, do NOT browse, search, or download it.", prompt)

    def test_tool_docs_route_existing_photos_to_search_and_send(self):
        from core.tool_defs import build_tool_definitions

        tools = build_tool_definitions(enabled_skills=[])
        by_name = {tool["function"]["name"]: tool["function"]["description"] for tool in tools}

        self.assertIn("image_search", by_name)
        self.assertIn("send_media", by_name)
        self.assertIn("send_media", by_name["image_search"])
        self.assertIn("image_search", by_name["send_media"])
        self.assertIn("Do NOT use this to download", by_name["generate_image"])
        self.assertIn("photo into the current chat", by_name["spawn_agent"])

    def test_shortlist_maps_send_picture_to_search_and_send_not_generate(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=[])
        selected = shortlist_tool_definitions(tools, ROSE_REQUEST)
        names = {tool["function"]["name"] for tool in selected}

        self.assertIn("image_search", names)
        self.assertIn("send_media", names)
        self.assertNotIn("generate_image", names)
        self.assertNotIn("spawn_agent", names)
        self.assertNotIn("capability_search", names)
        self.assertNotIn("run_command", names)

    def test_search_tools_are_registered_without_browser_or_keys(self):
        from core.tool_defs import build_tool_definitions

        names = {
            tool["function"]["name"]
            for tool in build_tool_definitions(enabled_skills=[], search_available=False)
        }
        self.assertIn("image_search", names)
        self.assertIn("web_search", names)


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


if __name__ == "__main__":
    unittest.main()
