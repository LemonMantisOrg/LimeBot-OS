import hashlib
import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace


class TestWorkspaceIsolation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source_root = Path("temp") / "workspace_isolation_tests"
        self.source_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.source_root, ignore_errors=True)

    async def test_file_tools_write_to_copy_and_capture_structured_diff(self):
        from core.bus import MessageBus
        from core.tools import Toolbox
        from core.workspace_isolation import IsolatedWorkspace

        source_file = self.source_root / "sample.py"
        source_file.write_text("def value():\n    return 1\n", encoding="utf-8")
        workspace = await IsolatedWorkspace.create(self.source_root, label="test")
        try:
            toolbox = Toolbox(
                allowed_paths=[str(Path.cwd())],
                bus=MessageBus(),
                config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
            )
            token = workspace.activate()
            try:
                read_result = await toolbox.read_file("sample.py", include_hash=True)
                expected = hashlib.sha256(
                    (workspace.root / "sample.py").read_bytes()
                ).hexdigest()
                self.assertIn(f"[File SHA-256: {expected}]", read_result)

                result = json.loads(
                    await toolbox.edit_file(
                        str(source_file),
                        [{"old_text": "return 1", "new_text": "return 2"}],
                        expected,
                    )
                )
                self.assertEqual(result["status"], "applied")
                self.assertEqual(
                    source_file.read_text(encoding="utf-8"),
                    "def value():\n    return 1\n",
                )
                self.assertEqual(
                    (workspace.root / "sample.py").read_text(encoding="utf-8"),
                    "def value():\n    return 2\n",
                )
                self.assertEqual(
                    json.loads(await toolbox.verify_files(["sample.py"]))["status"],
                    "passed",
                )

                capture = await workspace.capture()
                self.assertEqual(capture["status"], "changed")
                self.assertEqual(capture["summary"]["modified"], 1)
                self.assertIn("return 1", capture["changed_files"][0]["diff"])
                self.assertIn("return 2", capture["changed_files"][0]["diff"])
            finally:
                workspace.deactivate(token)
        finally:
            await workspace.cleanup()

        self.assertFalse(workspace.root.exists())

    async def test_isolation_denies_escape_and_blocks_live_command_paths(self):
        from core.bus import MessageBus
        from core.tools import Toolbox
        from core.workspace_isolation import IsolatedWorkspace

        source_file = self.source_root / "sample.txt"
        source_file.write_text("safe\n", encoding="utf-8")
        workspace = await IsolatedWorkspace.create(self.source_root, label="guard")
        try:
            toolbox = Toolbox(
                allowed_paths=[str(Path.cwd())],
                bus=MessageBus(),
                config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
            )
            token = workspace.activate()
            try:
                self.assertTrue(toolbox._is_path_allowed(str(source_file)))
                self.assertFalse(toolbox._is_path_allowed("/etc/passwd"))
                self.assertIn("cannot escape", toolbox.validate_command("type ..\\README.md"))
                self.assertIn(
                    "cannot address the live project",
                    toolbox.validate_command(f"type {source_file}"),
                )
                self.assertIsNone(
                    toolbox.validate_command("python -m unittest && python -m py_compile sample.txt")
                )
                self.assertIn(
                    "PYTHONPATH=",
                    toolbox.validate_command("PYTHONPATH=. python -m unittest") or "",
                )
            finally:
                workspace.deactivate(token)
        finally:
            await workspace.cleanup()

    async def test_live_tree_unchanged_until_apply_then_clone_gone(self):
        from core.bus import MessageBus
        from core.tools import Toolbox
        from core.workspace_isolation import IsolatedWorkspace

        source_file = self.source_root / "clamp.py"
        source_file.write_text("def clamp(n):\n    return n\n", encoding="utf-8")
        leftover = Path("temp") / "bakeoff-t4-isolated"
        leftover.mkdir(parents=True, exist_ok=True)
        (leftover / "stale.txt").write_text("leftover\n", encoding="utf-8")

        workspace = await IsolatedWorkspace.create(self.source_root, label="apply")
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd()), str(self.source_root)],
            bus=MessageBus(),
            config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
        )
        toolbox.allowed_paths = [Path.cwd().resolve(), self.source_root.resolve()]
        token = workspace.activate()
        try:
            expected = hashlib.sha256(
                (workspace.root / "clamp.py").read_bytes()
            ).hexdigest()
            result = json.loads(
                await toolbox.edit_file(
                    "clamp.py",
                    [{"old_text": "return n", "new_text": "return max(0, min(n, 10))"}],
                    expected,
                )
            )
            self.assertEqual(result["status"], "applied")
            self.assertEqual(
                source_file.read_text(encoding="utf-8"),
                "def clamp(n):\n    return n\n",
            )
            capture = await workspace.capture()
            self.assertEqual(capture["status"], "changed")
            workspace.retain()
        finally:
            workspace.deactivate(token)

        self.assertTrue(workspace.root.exists())
        applied = json.loads(
            await toolbox.apply_workspace_changeset(workspace_id=workspace.workspace_id)
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(
            source_file.read_text(encoding="utf-8"),
            "def clamp(n):\n    return max(0, min(n, 10))\n",
        )
        self.assertFalse(workspace.root.exists())
        self.assertFalse(leftover.exists())

    async def test_spawn_agent_retains_copy_until_apply(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        class FakeAgent:
            def __init__(self):
                self.workspace = None

            async def run_subagent(
                self,
                parent_session_key,
                sub_session_key,
                task,
                agent_name=None,
                isolated_workspace=None,
                isolation_mode="none",
            ):
                self.workspace = isolated_workspace
                token = isolated_workspace.activate()
                try:
                    (isolated_workspace.root / "created.txt").write_text(
                        "only in the copy\n", encoding="utf-8"
                    )
                finally:
                    isolated_workspace.deactivate(token)
                return f"finished in {isolation_mode}"

        fake_agent = FakeAgent()
        toolbox = Toolbox(
            allowed_paths=[str(self.source_root)],
            bus=MessageBus(),
            config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
        )
        toolbox.allowed_paths = [self.source_root.resolve()]
        toolbox.agent = fake_agent

        result = await toolbox.spawn_agent(
            "Implement the requested code fix",
            session_key="web:chat",
            isolation="copy",
        )

        self.assertIn("finished in copy", result)
        self.assertFalse((self.source_root / "created.txt").exists())
        self.assertIsNotNone(fake_agent.workspace)
        self.assertTrue(fake_agent.workspace.root.exists())
        self.assertTrue(fake_agent.workspace.is_pending())

        applied = json.loads(
            await toolbox.apply_workspace_changeset(
                workspace_id=fake_agent.workspace.workspace_id
            )
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(
            (self.source_root / "created.txt").read_text(encoding="utf-8"),
            "only in the copy\n",
        )
        self.assertFalse(fake_agent.workspace.root.exists())


if __name__ == "__main__":
    unittest.main()
