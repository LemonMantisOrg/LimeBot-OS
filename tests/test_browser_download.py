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


class TestBrowserExtractTables(unittest.TestCase):
    def test_python_downloads_table_keeps_312_patch_slug(self):
        from pathlib import Path

        from core.browser import compact_html_tables, merge_extract_text

        html = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "browser"
            / "python_downloads_table.html"
        ).read_text(encoding="utf-8")
        tables = compact_html_tables(html)
        self.assertIn("3.14", tables)
        self.assertIn("3.13", tables)
        self.assertIn("3.12", tables)
        self.assertIn("python-31214", tables)
        filler = "NAV " * 2000
        text, _truncated = merge_extract_text(filler, tables, limit=5000)
        self.assertIn("python-31214", text)
        self.assertIn("3.12", text)
        self.assertTrue(text.startswith("Active tables:"))
