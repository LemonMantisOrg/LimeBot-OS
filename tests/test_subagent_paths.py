import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.bus import MessageBus
from core.context import workspace_context
from core.tools import Toolbox


class TestSubagentAllowedPaths(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_subagent_can_read_parent_allowed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp)
            project = live / "project"
            extra = live / "notes"
            project.mkdir()
            extra.mkdir()
            (project / "AGENTS.md").write_text("# agents\nparent readable\n", encoding="utf-8")
            (extra / "standup.md").write_text("hello from temp\n", encoding="utf-8")
            clone = live / "clone"
            clone.mkdir()
            (clone / "only-copy.txt").write_text("clone\n", encoding="utf-8")

            toolbox = Toolbox(
                allowed_paths=[str(project), str(extra)],
                bus=MessageBus(),
                config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
            )
            token = workspace_context.set(
                {
                    "mode": "copy",
                    "root": str(clone),
                    "source_root": str(project),
                    "label": "explorer",
                }
            )
            try:
                agents = await toolbox.read_file(str(project / "AGENTS.md"))
                standup = await toolbox.read_file(str(extra / "standup.md"))
                local = await toolbox.read_file(str(clone / "only-copy.txt"))
            finally:
                workspace_context.reset(token)

            self.assertIn("parent readable", agents)
            self.assertIn("hello from temp", standup)
            self.assertIn("clone", local)
            self.assertNotIn("Access denied", agents)
            self.assertNotIn("Access denied", standup)
