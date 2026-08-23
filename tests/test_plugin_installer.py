import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.plugin_installer import PluginInstaller
from core.plugin_manifest import discover_plugins, parse_plugin_dir
from core.skills import SkillRegistry


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cursor-plugins"


class TestPluginManifest(unittest.TestCase):
    def test_parses_official_github_plugin_fixture(self):
        manifest = parse_plugin_dir(FIXTURES / "github")
        self.assertEqual(manifest.name, "github")
        self.assertIn("github", manifest.mcp_servers)
        self.assertEqual(manifest.mcp_servers["github"]["type"], "http")

    def test_parses_official_create_plugin_skills_and_rules(self):
        manifest = parse_plugin_dir(FIXTURES / "create-plugin")
        self.assertEqual(manifest.name, "create-plugin")
        self.assertTrue(any(path.name == "create-plugin-scaffold" for path in manifest.skills))
        self.assertTrue(manifest.rules)

    def test_rejects_missing_manifest(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                parse_plugin_dir(tmp)


class TestPluginInstall(unittest.TestCase):
    def test_local_create_plugin_install_loads_skill(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer = PluginInstaller(
                plugins_dir=root / "plugins",
                config_file=root / "limebot.json",
            )
            result = installer.install(str(FIXTURES / "create-plugin"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["installed"][0]["skills"], ["create-plugin-scaffold"])

            registry = SkillRegistry(
                skill_dirs=[str(root / "plugins" / "create-plugin" / "skills")],
                config={"skills": {"enabled": ["create-plugin-scaffold"]}},
            )
            registry.discover_and_load()
            self.assertIn("create-plugin-scaffold", registry.skills)
            additions = registry.get_system_prompt_additions()
            self.assertIn("create-plugin-scaffold", additions)

    def test_local_github_plugin_install_records_mcp_server(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer = PluginInstaller(
                plugins_dir=root / "plugins",
                config_file=root / "limebot.json",
            )
            from core import mcp_client

            original = mcp_client.CONFIG_PATH
            mcp_client.CONFIG_PATH = root / "mcp_config.json"
            try:
                result = installer.install(str(FIXTURES / "github"))
                self.assertTrue(result["ok"])
                self.assertTrue(result["installed"][0]["mcp_servers"])
                saved = json.loads((root / "mcp_config.json").read_text(encoding="utf-8"))
                self.assertIn("github_github", saved["mcpServers"])
                self.assertEqual(
                    saved["mcpServers"]["github_github"]["url"],
                    "https://api.githubcopilot.com/mcp/",
                )
            finally:
                mcp_client.CONFIG_PATH = original

    def test_discover_plugins_returns_one_package(self):
        packages = discover_plugins(FIXTURES / "create-plugin")
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].name, "create-plugin")
