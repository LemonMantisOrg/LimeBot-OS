import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


class TestSkillEditor(unittest.TestCase):
    @staticmethod
    def _make_registry(root: Path, name: str = "demo_skill"):
        from core.skills import SkillRegistry

        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: A test skill\n---\nUse the skill.\n",
            encoding="utf-8",
        )
        (skill_dir / "main.py").write_text(
            "def run():\n    return 'before'\n",
            encoding="utf-8",
        )
        (skill_dir / "api.py").write_text(
            "def handle(*args, **kwargs):\n    return {'ok': True}\n",
            encoding="utf-8",
        )
        registry = SkillRegistry(
            skill_dirs=[str(root)],
            config={"skills": {"enabled": [name]}},
        )
        registry.discover_and_load()
        return registry, skill_dir

    def test_inspection_and_transactional_edit_reload_local_skill(self):
        from core.skill_editor import SkillEditor

        with TemporaryDirectory() as temporary:
            registry, skill_dir = self._make_registry(Path(temporary))
            editor = SkillEditor(registry)

            inspected = editor.inspect("demo_skill", path="main.py")
            self.assertEqual(inspected["code"], "skill_inspected")
            self.assertEqual(inspected["source_kind"], "legacy-local")
            self.assertTrue(inspected["editable"])
            self.assertIn("before", inspected["content"])
            self.assertNotIn(str(skill_dir), json.dumps(inspected))

            result = editor.edit(
                "demo_skill",
                [
                    {
                        "operation": "replace",
                        "path": "main.py",
                        "old_text": "return 'before'",
                        "new_text": "return 'after'",
                    }
                ],
            )
            self.assertEqual(result["code"], "skill_reloaded")
            self.assertEqual((skill_dir / "main.py").read_text(encoding="utf-8").strip().splitlines()[-1], "    return 'after'")
            self.assertTrue(registry.skills["demo_skill"]["active"])

    def test_invalid_python_rolls_back_without_partial_changes(self):
        from core.skill_editor import SkillEditError, SkillEditor

        with TemporaryDirectory() as temporary:
            registry, skill_dir = self._make_registry(Path(temporary))
            editor = SkillEditor(registry)
            original = (skill_dir / "main.py").read_text(encoding="utf-8")

            with self.assertRaises(SkillEditError) as raised:
                editor.edit(
                    "demo_skill",
                    [
                        {
                            "operation": "replace",
                            "path": "main.py",
                            "old_text": "return 'before'",
                            "new_text": "return (",
                        },
                        {
                            "operation": "create",
                            "path": "extra.json",
                            "content": '{"valid": true}',
                        },
                    ],
                )
            self.assertEqual(raised.exception.code, "validation_failed")
            self.assertEqual((skill_dir / "main.py").read_text(encoding="utf-8"), original)
            self.assertFalse((skill_dir / "extra.json").exists())

    def test_stale_exact_match_is_rejected(self):
        from core.skill_editor import SkillEditError, SkillEditor

        with TemporaryDirectory() as temporary:
            registry, skill_dir = self._make_registry(Path(temporary))
            editor = SkillEditor(registry)
            (skill_dir / "main.py").write_text(
                "def run():\n    return 'changed'\n", encoding="utf-8"
            )
            with self.assertRaises(SkillEditError) as raised:
                editor.edit(
                    "demo_skill",
                    [
                        {
                            "operation": "replace",
                            "path": "main.py",
                            "old_text": "return 'before'",
                            "new_text": "return 'after'",
                        }
                    ],
                )
            self.assertEqual(raised.exception.code, "stale_match")

    def test_bundled_skill_is_inspectable_but_read_only(self):
        from core.runtime_paths import PROJECT_DIR
        from core.skill_editor import SkillEditError, SkillEditor
        from core.skills import SkillRegistry

        registry = SkillRegistry(
            skill_dirs=[str(PROJECT_DIR / "skills")],
            config={"skills": {"enabled": []}},
        )
        registry.discover_and_load()
        if "browser" not in registry.skills:
            self.skipTest("bundled browser skill is not present")
        editor = SkillEditor(registry)
        inspected = editor.inspect("browser")
        self.assertEqual(inspected["source_kind"], "bundled")
        self.assertFalse(inspected["editable"])
        with self.assertRaises(SkillEditError) as raised:
            editor.edit(
                "browser",
                [
                    {
                        "operation": "replace",
                        "path": "SKILL.md",
                        "old_text": "browser",
                        "new_text": "changed",
                    }
                ],
            )
        self.assertEqual(raised.exception.code, "skill_read_only")

    def test_git_managed_skill_is_read_only_even_in_local_storage(self):
        from core.skill_editor import SkillEditError, SkillEditor
        from core.skills import SkillRegistry

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_dir = root / "git_skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: git_skill\ndescription: Git skill\n---\nManual.\n",
                encoding="utf-8",
            )
            registry = SkillRegistry(
                skill_dirs=[str(root)],
                config={
                    "skills": {
                        "enabled": [],
                        "installed": {"git_skill": {"source": "git"}},
                    }
                },
            )
            registry.discover_and_load()
            self.assertEqual(registry.skills["git_skill"]["source_kind"], "git-managed")
            self.assertFalse(registry.skills["git_skill"]["editable"])
            with self.assertRaises(SkillEditError) as raised:
                SkillEditor(registry).edit(
                    "git_skill",
                    [{"operation": "replace", "path": "SKILL.md", "old_text": "Manual.", "new_text": "Changed."}],
                )
            self.assertEqual(raised.exception.code, "skill_read_only")

    def test_create_skill_uses_local_storage_and_persists_enablement(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skills"
            config_path = root / "limebot.json"
            config_path.write_text(
                json.dumps({"skills": {"enabled": [], "installed": {}}}),
                encoding="utf-8",
            )
            config = SimpleNamespace(skills=SimpleNamespace(enabled=[]))
            toolbox = Toolbox([str(root)], MessageBus(), config)
            with patch("core.tools.get_skills_dir", return_value=skill_root), patch(
                "core.tools.get_config_file", return_value=config_path
            ):
                result = json.loads(
                    awaitable_result(
                        toolbox.create_skill("new_skill", "A local test skill")
                    )
                )
            self.assertEqual(result["code"], "skill_created")
            self.assertTrue((skill_root / "new_skill" / "SKILL.md").is_file())
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["skills"]["enabled"], ["new_skill"])


def awaitable_result(awaitable):
    """Run one toolbox coroutine in the test's synchronous test method."""

    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
