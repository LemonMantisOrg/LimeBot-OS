import tempfile
import unittest
from pathlib import Path

from core.browser import BROWSER_INSTALL_HINT, BrowserManager, browser_unavailable_message


class TestBrowserDownloadHelpers(unittest.TestCase):
    def test_missing_playwright_message_is_one_setup_command(self):
        text = browser_unavailable_message()
        self.assertEqual(text, BROWSER_INSTALL_HINT)
        self.assertIn("setup -- --recommended", text)
        self.assertNotIn("Traceback", text)

    def test_download_dest_stays_under_temp(self):
        manager = BrowserManager.__new__(BrowserManager)
        manager.downloads_dir = Path.cwd() / "temp" / "downloads" / "browser"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "outside.bin"
            with self.assertRaises(ValueError):
                manager._resolve_download_dest("file.bin", str(dest))

        allowed = Path.cwd() / "temp" / "downloads" / "browser" / "iso.bin"
        resolved = manager._resolve_download_dest("iso.bin", str(allowed))
        self.assertEqual(resolved, allowed.resolve())
