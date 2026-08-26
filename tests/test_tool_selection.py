import sys
import types
import json
import unittest
from types import SimpleNamespace

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

if "loguru" not in sys.modules:
    loguru = types.ModuleType("loguru")

    class _DummyLogger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    loguru.logger = _DummyLogger()
    sys.modules["loguru"] = loguru


class TestToolSelection(unittest.TestCase):
    def _require_fast_harness_loop_integration(self):
        from core.loop import AgentLoop

        has_turn_method = hasattr(AgentLoop, "_should_include_tools_for_turn")
        if not has_turn_method:
            self.skipTest(
                "Requires core/loop.py integration for fast AI harness tool routing."
            )
        return AgentLoop

    def test_should_include_tools_for_plain_directory_request(self):
        from core.loop import AgentLoop

        self.assertTrue(AgentLoop._should_include_tools("list your current dir"))
        self.assertTrue(
            AgentLoop._should_include_tools("show me the working directory")
        )

    def test_should_include_tools_for_plain_smalltalk(self):
        from core.loop import AgentLoop

        self.assertTrue(AgentLoop._should_include_tools("how are you today"))

    def test_shortlist_prefers_filesystem_cluster_for_code_search(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        shortlisted = shortlist_tool_definitions(
            tools, "find verify_auth in the codebase and read the file"
        )
        names = {tool["function"]["name"] for tool in shortlisted}

        self.assertIn("search_files", names)
        self.assertIn("read_file", names)
        self.assertIn("list_dir", names)
        self.assertNotIn("google_search", names)

    def test_shortlist_prefers_browser_cluster_for_urls(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        shortlisted = shortlist_tool_definitions(
            tools, "open https://example.com and inspect the page"
        )
        names = {tool["function"]["name"] for tool in shortlisted}

        self.assertIn("browser_navigate", names)
        self.assertIn("browser_act", names)
        self.assertNotIn("browser_click", names)
        self.assertNotIn("read_file", names)

    def test_spanish_browser_export_workflow_keeps_complete_tool_chain(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        prompt = (
            "Utilice la calculadora de Azure https://azure.microsoft.com/es-es/pricing/calculator/ "
            "para calcular una máquina virtual, exportar la hoja de Excel y adjuntar una captura."
        )

        shortlisted = shortlist_tool_definitions(tools, prompt)
        names = {tool["function"]["name"] for tool in shortlisted}

        self.assertLessEqual(len(shortlisted), 12)
        self.assertTrue(
            {
                "browser_navigate",
                "browser_act",
                "browser_extract",
                "run_command",
                "list_dir",
                "read_file",
                "send_media",
            }
            <= names
        )

    def test_price_research_excel_workflow_keeps_native_math_and_workbook_tools(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        shortlisted = shortlist_tool_definitions(
            tools,
            "Search current Azure VPS prices, calculate monthly totals, create an Excel workbook, and send the file",
        )
        names = {tool["function"]["name"] for tool in shortlisted}

        self.assertLessEqual(len(shortlisted), 12)
        self.assertIn("web_search", names)
        self.assertIn("calculate", names)
        self.assertIn("create_spreadsheet", names)
        self.assertIn("send_media", names)

    def test_explicit_url_and_research_requests_require_initial_tool_use(self):
        AgentLoop = self._require_fast_harness_loop_integration()
        tools = [
            {"function": {"name": "browser_navigate"}},
            {"function": {"name": "web_search"}},
        ]

        self.assertTrue(
            AgentLoop._requires_initial_tool_call(
                "Analice https://example.com y haga un resumen", tools
            )
        )
        self.assertTrue(
            AgentLoop._requires_initial_tool_call(
                "Investigue los precios actuales de Azure", tools
            )
        )
        self.assertFalse(
            AgentLoop._requires_initial_tool_call("Explique qué es un servidor", tools)
        )

    def test_generate_image_schema_supports_chat_references(self):
        from core.tool_defs import build_tool_definitions

        tools = build_tool_definitions(enabled_skills=[])
        generate = next(
            tool for tool in tools if tool["function"]["name"] == "generate_image"
        )
        properties = generate["function"]["parameters"]["properties"]

        self.assertEqual(properties["reference_images"]["type"], "array")
        self.assertEqual(properties["use_attached_images"]["type"], "boolean")

    def test_agent_normalizes_common_tool_aliases(self):
        from core.loop import AgentLoop
        from core.metrics import MetricsCollector

        agent = object.__new__(AgentLoop)
        agent.metrics = MetricsCollector()
        agent._filesystem_alias_actions = {
            "list": "list_dir",
            "read": "read_file",
            "write": "write_file",
            "delete": "delete_file",
            "find": "search_files",
            "search": "search_files",
        }
        agent._tool_name_aliases = {
            "ls": "list_dir",
            "dir": "list_dir",
            "list_files": "list_dir",
            "cat": "read_file",
            "open_file": "read_file",
            "show_file": "read_file",
            "grep": "search_files",
            "rg": "search_files",
            "ripgrep": "search_files",
            "find_files": "search_files",
            "shell": "run_command",
            "terminal": "run_command",
            "exec": "run_command",
            "bash": "run_command",
            "powershell": "run_command",
            "cmd": "run_command",
        }

        name, args = agent._normalize_tool_alias("grep", {"pattern": "TODO"}, "web:test")
        self.assertEqual(name, "search_files")
        self.assertEqual(args["query"], "TODO")

        name, args = agent._normalize_tool_alias("cat", {"file": "README.md"}, "web:test")
        self.assertEqual(name, "read_file")
        self.assertEqual(args["path"], "README.md")

        name, args = agent._normalize_tool_alias(
            "powershell", {"script": "git status"}, "web:test"
        )
        self.assertEqual(name, "run_command")
        self.assertEqual(args["command"], "git status")

    def test_agent_normalizes_json_suffixed_read_aliases(self):
        from core.loop import AgentLoop
        from core.metrics import MetricsCollector

        agent = object.__new__(AgentLoop)
        agent.metrics = MetricsCollector()

        name, args = agent._normalize_tool_alias(
            "read_filejson",
            {"path": "core/asyncio_compat.py", "line_start": 1, "line_end": 40},
            "web:test",
        )
        self.assertEqual(name, "read_file")
        self.assertEqual(args["path"], "core/asyncio_compat.py")
        self.assertEqual(args["start_line"], 1)
        self.assertEqual(args["end_line"], 40)

    def test_agent_uses_full_tool_schema_when_shortlist_disabled_in_fast_mode(self):
        from core.loop import AgentLoop

        agent = object.__new__(AgentLoop)
        all_tools = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "browser_navigate"}},
            {"function": {"name": "list_dir"}},
        ]
        agent._get_tool_definitions = lambda: all_tools
        agent.config = SimpleNamespace(
            tool_shortlist_enabled=False,
            ai_harness=SimpleNamespace(mode="fast"),
        )
        agent._log_tool_debug = lambda *args, **kwargs: None

        selected = agent._get_tool_definitions_for_turn(
            "open https://example.com and inspect the page"
        )

        self.assertEqual(selected, all_tools)

    def test_agent_uses_normalized_shortlist_config(self):
        from core.loop import AgentLoop
        from core.tool_defs import shortlist_tool_definitions

        agent = object.__new__(AgentLoop)
        all_tools = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "browser_navigate"}},
            {"function": {"name": "browser_act"}},
            {"function": {"name": "list_dir"}},
        ]
        agent._get_tool_definitions = lambda: all_tools
        agent.config = SimpleNamespace(tool_shortlist_enabled=True)
        agent._log_tool_debug = lambda *args, **kwargs: None

        selected = agent._get_tool_definitions_for_turn(
            "open https://example.com and inspect the page"
        )

        self.assertEqual(
            selected,
            shortlist_tool_definitions(
                all_tools, "open https://example.com and inspect the page"
            ),
        )

    def test_fast_harness_can_disable_tools_for_casual_smalltalk(self):
        AgentLoop = self._require_fast_harness_loop_integration()

        agent = object.__new__(AgentLoop)
        agent.config = SimpleNamespace(
            tool_shortlist_enabled=True,
            ai_harness=SimpleNamespace(
                mode="fast", fast_disable_tools_for_casual=True
            )
        )

        self.assertFalse(agent._should_include_tools_for_turn("how are you today"))
        self.assertFalse(agent._should_include_tools_for_turn("ok"))

    def test_fast_harness_keeps_tools_for_clear_action_requests(self):
        AgentLoop = self._require_fast_harness_loop_integration()

        agent = object.__new__(AgentLoop)
        agent.config = SimpleNamespace(
            tool_shortlist_enabled=True,
            ai_harness=SimpleNamespace(
                mode="fast", fast_disable_tools_for_casual=True
            )
        )

        self.assertTrue(agent._should_include_tools_for_turn("list your current dir"))
        self.assertTrue(
            agent._should_include_tools_for_turn(
                "open https://example.com and inspect the page"
            )
        )
        self.assertTrue(agent._should_include_tools_for_turn("read ./README.md"))

    def test_fast_harness_shortlists_when_explicitly_enabled(self):
        AgentLoop = self._require_fast_harness_loop_integration()
        from core.tool_defs import shortlist_tool_definitions

        agent = object.__new__(AgentLoop)
        agent.config = SimpleNamespace(
            tool_shortlist_enabled=True,
            ai_harness=SimpleNamespace(
                mode="fast", fast_disable_tools_for_casual=True
            )
        )
        agent._get_tool_definitions = lambda: [
            {"function": {"name": "read_file"}},
            {"function": {"name": "browser_navigate"}},
            {"function": {"name": "browser_act"}},
            {"function": {"name": "list_dir"}},
        ]
        agent._log_tool_debug = lambda *args, **kwargs: None

        selected = agent._get_tool_definitions_for_turn(
            "open https://example.com and inspect the page"
        )

        self.assertEqual(
            selected,
            shortlist_tool_definitions(
                agent._get_tool_definitions(),
                "open https://example.com and inspect the page",
            ),
        )

    def test_agent_adds_forced_skill_required_tools_to_shortlist(self):
        AgentLoop = self._require_fast_harness_loop_integration()
        from core.tool_defs import build_tool_definitions

        agent = object.__new__(AgentLoop)
        agent.config = SimpleNamespace(tool_shortlist_enabled=True)
        agent.skill_registry = SimpleNamespace(
            get_required_tool_names=lambda name: (
                ["run_command", "send_media"] if name == "docx-creator" else []
            )
        )
        agent._get_tool_definitions = lambda: build_tool_definitions(
            enabled_skills=[]
        )
        agent._log_tool_debug = lambda *args, **kwargs: None

        selected = agent._get_tool_definitions_for_turn(
            "use this skill for that, also in APA please",
            forced_skill_name="docx-creator",
        )
        names = {tool["function"]["name"] for tool in selected}

        self.assertTrue({"run_command", "send_media"} <= names)

    def test_actual_registry_routing_matrix_is_bounded_and_capable(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=["browser"])
        cases = {
            "find and read config.py in the repository": {"search_files", "read_file"},
            "write changes to core/loop.py": {"write_file"},
            "delete temp/output.json": {"delete_file"},
            "run pytest for the tests": {"run_command"},
            "open https://example.com then click and type into the form": {
                "browser_navigate", "browser_act"
            },
            "search the web for current Python news": {"web_search"},
            "find an image of a lime": {"web_search"},
            "deep research this topic with sources": {"web_search"},
            "recall what I told you yesterday": {"memory_search"},
            "remind me tomorrow at noon": {"cron_add"},
            "send a Discord DM": {"send_discord_message"},
            "send this photo as an attachment": {"web_search"},
            "send a voice note": {"send_voice"},
            "generate an image of a lime": {"generate_image"},
            "download a picture of rose of blackpink for me and send me that in this chat": {
                "web_search",
            },
            "delegate this to a subagent": {"spawn_agent"},
        }
        exclusive_prompts = {
            "find an image of a lime",
            "send this photo as an attachment",
            "generate an image of a lime",
            "download a picture of rose of blackpink for me and send me that in this chat",
        }
        for prompt, required in cases.items():
            with self.subTest(prompt=prompt):
                selected = shortlist_tool_definitions(tools, prompt, channel="web")
                names = {tool["function"]["name"] for tool in selected}
                self.assertTrue(required <= names, (required, names))
                self.assertLessEqual(len(selected), 12)
                if prompt in exclusive_prompts:
                    self.assertEqual(names, required)
                else:
                    self.assertLess(len(json.dumps(selected)), len(json.dumps(tools)))

        ambiguous = "help me with this"
        self.assertEqual(shortlist_tool_definitions(tools, ambiguous), tools)

    def test_shortlist_preserves_tools_required_by_forced_skill(self):
        from core.tool_defs import build_tool_definitions, shortlist_tool_definitions

        tools = build_tool_definitions(enabled_skills=[])
        shortlisted = shortlist_tool_definitions(
            tools,
            "use this skill for that, also in APA please",
            required_tool_names={"read_file", "list_dir", "run_command", "send_media"},
        )
        names = {tool["function"]["name"] for tool in shortlisted}

        self.assertTrue(
            {"read_file", "list_dir", "run_command", "send_media"} <= names
        )
        self.assertLessEqual(len(shortlisted), 12)
