import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace


class TestOptionalDiagnostics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path("temp") / "diagnostics_tests"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_missing_diagnostics_provider_is_a_clean_skip(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        path = self.root / "sample.py"
        path.write_text("value = 1\n", encoding="utf-8")
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())],
            bus=MessageBus(),
            config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
        )

        result = json.loads(await toolbox.diagnose_files([str(path)], provider="none"))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["provider"], "none")

        verified = json.loads(
            await toolbox.verify_files(
                [str(path)], include_diagnostics=True, provider="none"
            )
        )
        self.assertEqual(verified["status"], "passed")
        self.assertEqual(verified["diagnostics"]["status"], "skipped")

    async def test_unknown_provider_is_reported_as_failed(self):
        from core.bus import MessageBus
        from core.tools import Toolbox

        path = self.root / "sample.py"
        path.write_text("value = 1\n", encoding="utf-8")
        toolbox = Toolbox(
            allowed_paths=[str(Path.cwd())],
            bus=MessageBus(),
            config=SimpleNamespace(skills=SimpleNamespace(enabled=[])),
        )

        result = json.loads(await toolbox.diagnose_files([str(path)], provider="bogus"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Unknown diagnostics provider", result["detail"])


if __name__ == "__main__":
    unittest.main()
