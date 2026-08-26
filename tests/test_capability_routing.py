import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


class TestCapabilityRouting(unittest.TestCase):
    @staticmethod
    def _jira_registry(root: Path):
        from core.skills import SkillRegistry

        skill_dir = root / "jira"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: jira\n"
            "description: Manage Jira tickets and issues\n"
            "metadata:\n"
            "  aliases: [tickets, issue tracker]\n"
            "  required_tools: [run_command, read_file]\n"
            "---\n"
            "Use this skill for Jira tickets and issue updates.\n",
            encoding="utf-8",
        )
        registry = SkillRegistry(
            skill_dirs=[str(root)],
            config={"skills": {"enabled": ["jira"]}},
        )
        registry.discover_and_load()
        return registry

    def test_catalog_exposes_lifecycle_state_and_required_tools(self):
        with TemporaryDirectory() as temp_dir:
            registry = self._jira_registry(Path(temp_dir))

            catalog = registry.get_capability_catalog()

            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0]["name"], "jira")
            self.assertEqual(catalog[0]["state"], "ready")
            self.assertTrue(catalog[0]["discovered"])
            self.assertTrue(catalog[0]["enabled"])
            self.assertTrue(catalog[0]["dependencies_ready"])
            self.assertEqual(
                catalog[0]["required_tools"], ["run_command", "read_file"]
            )
            self.assertEqual(
                registry.search_capabilities("GDHD-1199 Jira ticket")[0]["name"],
                "jira",
            )

    def test_catalog_distinguishes_enabled_from_missing_declared_credentials(self):
        from core.skills import SkillRegistry

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "jira"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: jira\n"
                "description: Jira integration\n"
                "metadata:\n"
                "  required_env: [JIRA_API_TOKEN]\n"
                "---\nUse Jira.\n",
                encoding="utf-8",
            )
            registry = SkillRegistry(
                skill_dirs=[str(root)],
                config={"skills": {"enabled": ["jira"]}},
            )
            with patch.dict("os.environ", {"JIRA_API_TOKEN": ""}, clear=False):
                registry.discover_and_load()

            item = registry.get_capability_catalog()[0]
            self.assertTrue(item["enabled"])
            self.assertFalse(item["configured"])
            self.assertEqual(item["state"], "configuration_missing")
            self.assertEqual(item["configuration_missing"], ["JIRA_API_TOKEN"])

    def test_rediscovery_replaces_removed_skill_and_advances_snapshot_revision(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = self._jira_registry(root)
            first_revision = registry.capability_revision
            for path in (root / "jira").iterdir():
                path.unlink()
            (root / "jira").rmdir()

            registry.discover_and_load()

            self.assertGreater(registry.capability_revision, first_revision)
            self.assertEqual(registry.get_capability_catalog(), [])
            self.assertEqual(registry.search_capabilities("Jira"), [])

    def test_short_followup_keeps_active_skill_and_required_tools(self):
        from core.loop import AgentLoop
        from core.tool_defs import build_tool_definitions

        with TemporaryDirectory() as temp_dir:
            registry = self._jira_registry(Path(temp_dir))
            agent = object.__new__(AgentLoop)
            agent.skill_registry = registry
            agent._session_capability_state = {}
            agent.config = SimpleNamespace(tool_shortlist_enabled=True)
            agent._get_tool_definitions = lambda: build_tool_definitions(
                enabled_skills=[]
            )
            agent._log_tool_debug = lambda *args, **kwargs: None

            first = agent._get_tool_definitions_for_turn(
                "Apply these changes to Jira ticket GDHD-1199",
                session_key="web:test",
            )
            second = agent._get_tool_definitions_for_turn(
                "Sí la tienes",
                session_key="web:test",
            )
            first_names = {tool["function"]["name"] for tool in first}
            second_names = {tool["function"]["name"] for tool in second}

            self.assertEqual(
                agent._session_capability_state["web:test"]["skill_names"],
                ["jira"],
            )
            self.assertTrue({"run_command", "read_file"} <= first_names)
            self.assertTrue({"run_command", "read_file"} <= second_names)
            self.assertNotIn("capability_search", second_names)
            self.assertTrue(
                agent._should_include_tools_for_turn("Sí la tienes", "web:test")
            )

    def test_capability_search_returns_match_and_resolution_reason(self):
        from core.loop import AgentLoop
        from core.tool_defs import build_tool_definitions

        with TemporaryDirectory() as temp_dir:
            registry = self._jira_registry(Path(temp_dir))
            agent = object.__new__(AgentLoop)
            agent.skill_registry = registry
            agent._session_capability_state = {}
            agent.config = SimpleNamespace(tool_shortlist_enabled=True)
            agent._get_tool_definitions = lambda: build_tool_definitions(
                enabled_skills=[]
            )
            agent._log_tool_debug = lambda *args, **kwargs: None
            agent.subagent_registry = SimpleNamespace(
                get_agent_descriptions=lambda: {"reviewer": "Review code"}
            )

            result = agent.resolve_capabilities("Jira ticket GDHD-1199")

            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["matched_skills"], ["jira"])
            self.assertEqual(result["required_tools"], ["run_command", "read_file"])
            self.assertTrue(
                any(
                    row["name"] == "jira" and row["type"] == "skill"
                    for row in result["capabilities"]
                )
            )
            self.assertNotIn("capability_search", result["selected_tools"])

    def test_capability_search_is_not_a_model_tool(self):
        from core.cache import ToolCache
        from core.loop import AgentLoop
        from core.tool_defs import build_tool_definitions

        names = {
            tool["function"]["name"]
            for tool in build_tool_definitions(enabled_skills=[])
        }
        self.assertNotIn("capability_search", names)

        agent = object.__new__(AgentLoop)
        agent.skill_registry = None
        agent._session_capability_state = {}
        agent.config = SimpleNamespace(tool_shortlist_enabled=True)
        agent._get_tool_definitions = lambda: build_tool_definitions(enabled_skills=[])
        agent._log_tool_debug = lambda *args, **kwargs: None
        agent.tool_cache = ToolCache()

        import asyncio

        raw = asyncio.run(
            agent._execute_tool(
                "capability_search",
                {"query": "Jira ticket", "include_disabled": True},
                "web:test",
            )
        )
        self.assertTrue(str(raw).startswith("Error:"))
        self.assertIn("not available", str(raw))

    def test_explicit_research_requests_require_initial_tool_call(self):
        from core.loop import AgentLoop

        tools = [{"function": {"name": "run_command"}}]

        self.assertTrue(
            AgentLoop._requires_initial_tool_call(
                "Investigate the current Jira ticket", tools
            )
        )
        self.assertFalse(
            AgentLoop._requires_initial_tool_call("Explain what Jira is", tools)
        )

    def test_web_diagnostic_endpoint_delegates_to_live_agent(self):
        from fastapi.testclient import TestClient
        from channels.web import WebChannel
        from core.bus import MessageBus

        config = SimpleNamespace(
            whitelist=SimpleNamespace(api_key=None, allowed_paths=[]),
            web=SimpleNamespace(port=8000, allowed_origins=[]),
            llm=SimpleNamespace(model="openai/gpt-4o", base_url=""),
        )
        channel = WebChannel(config=config, bus=MessageBus())
        channel.agent = SimpleNamespace(
            resolve_capabilities=lambda text, session_key=None: {
                "query": text,
                "state": "ready",
                "session_key": session_key,
            }
        )

        response = TestClient(channel.app).get(
            "/api/capabilities/resolve",
            params={"text": "apply changes to Jira", "session_key": "web:test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["query"], "apply changes to Jira")
        self.assertEqual(response.json()["session_key"], "web:test")
