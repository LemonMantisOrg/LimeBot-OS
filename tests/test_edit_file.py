import hashlib
import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace


class TestExactFileEdits(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path("temp") / "edit_file_tests"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_apply_text_edits_preflights_all_operations(self):
        from core.file_edits import EditValidationError, apply_text_edits

        original = "alpha = 1\nbeta = 2\n"
        updated, count = apply_text_edits(
            original,
            [
                {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                {"old_text": "beta = 2", "new_text": "beta = 20"},
            ],
        )
        self.assertEqual(count, 2)
        self.assertEqual(updated, "alpha = 10\nbeta = 20\n")

        with self.assertRaises(EditValidationError):
            apply_text_edits(
                original,
                [
                    {"old_text": "alpha = 1", "new_text": "alpha = 10"},
                    {"old_text": "missing", "new_text": "never lands"},
                ],
            )

    async def test_edit_file_applies_atomic_hash_guarded_patch(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        path = self.root / "sample.py"
        path.write_text("def value():\n    return 1\n", encoding="utf-8")
        config = SimpleNamespace(skills=SimpleNamespace(enabled=[]))
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())], bus=MessageBus(), config=config
        )
        expected = hashlib.sha256(path.read_bytes()).hexdigest()

        result = await toolbox.edit_file(
            str(path),
            [
                {
                    "old_text": "    return 1",
                    "new_text": "    return 2",
                }
            ],
            expected,
        )

        payload = json.loads(result)
        self.assertEqual(payload["status"], "applied")
        self.assertEqual(payload["replacements"], 1)
        self.assertEqual(payload["verification"]["status"], "passed")
        self.assertEqual(path.read_text(encoding="utf-8"), "def value():\n    return 2\n")

    async def test_edit_file_rejects_stale_and_partial_patches(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        path = self.root / "stale.py"
        path.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
        config = SimpleNamespace(skills=SimpleNamespace(enabled=[]))
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())], bus=MessageBus(), config=config
        )
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        path.write_text("first = 9\nsecond = 2\n", encoding="utf-8")

        stale = await toolbox.edit_file(
            str(path),
            [{"old_text": "second = 2", "new_text": "second = 3"}],
            expected,
        )
        self.assertIn("Stale edit rejected", stale)
        self.assertEqual(path.read_text(encoding="utf-8"), "first = 9\nsecond = 2\n")

        fresh = hashlib.sha256(path.read_bytes()).hexdigest()
        rejected = await toolbox.edit_file(
            str(path),
            [
                {"old_text": "first = 9", "new_text": "first = 10"},
                {"old_text": "does not exist", "new_text": "bad"},
            ],
            fresh,
        )
        self.assertIn("could not find", rejected)
        self.assertEqual(path.read_text(encoding="utf-8"), "first = 9\nsecond = 2\n")

    async def test_read_file_can_supply_edit_hash(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        path = self.root / "hash.txt"
        path.write_text("hash me", encoding="utf-8")
        config = SimpleNamespace(skills=SimpleNamespace(enabled=[]))
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())], bus=MessageBus(), config=config
        )

        result = await toolbox.read_file(str(path), include_hash=True)

        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIn(f"[File SHA-256: {expected}]", result)
        self.assertIn("hash me", result)

    async def test_verify_files_reports_syntax_and_conflict_errors(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        good = self.root / "good.py"
        bad = self.root / "bad.py"
        good.write_text("value = 1\n", encoding="utf-8")
        bad.write_text("<<<<<<< ours\nvalue = 1\n=======\nvalue = 2\n>>>>>>> theirs\n", encoding="utf-8")
        config = SimpleNamespace(skills=SimpleNamespace(enabled=[]))
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())], bus=MessageBus(), config=config
        )

        result = json.loads(await toolbox.verify_files([str(good), str(bad)]))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["files"][0]["status"], "passed")
        self.assertEqual(result["files"][1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
