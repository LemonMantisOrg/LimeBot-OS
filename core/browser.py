"""
Browser automation module using Playwright.
Provides session-scoped Chrome sessions with accessibility tree navigation.
Enhanced with human-like interactions to bypass bot detection.
"""

import asyncio
import hashlib
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None
    Browser = Any
    BrowserContext = Any
    Page = Any
    logger.warning("Playwright not installed. Browser features will be disabled.")

BROWSER_INSTALL_HINT = (
    "The real browser is not installed yet. One step from the LimeBot folder: "
    "npm run lime-bot setup -- --recommended"
    " (or: npm run lime-bot feature install browser && npm run install-browser)."
)


def browser_unavailable_message() -> str:
    return BROWSER_INSTALL_HINT


_VALID_BROWSER_MODES = frozenset({"isolated", "shared", "system", "attach"})


def _browser_settings(config: Any) -> Dict[str, str]:
    browser = getattr(config, "browser", None)
    mode = str(getattr(browser, "mode", "") or "isolated").strip().lower()
    channel = str(getattr(browser, "channel", "") or "").strip().lower()
    cdp_url = str(getattr(browser, "cdp_url", "") or "").strip()
    user_data_dir = str(getattr(browser, "user_data_dir", "") or "").strip()
    profile_directory = str(getattr(browser, "profile_directory", "") or "").strip()

    if mode not in _VALID_BROWSER_MODES:
        mode = "isolated"

    if mode == "attach":
        if not cdp_url:
            cdp_url = "http://127.0.0.1:9222"
    else:
        cdp_url = ""

    return {
        "mode": mode,
        "channel": channel,
        "cdp_url": cdp_url,
        "user_data_dir": user_data_dir,
        "profile_directory": profile_directory,
    }


def _default_system_profile_candidates() -> List[tuple[str, Path]]:
    if sys.platform == "win32":
        local = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return [
            ("chrome", local / "Google" / "Chrome" / "User Data"),
            ("msedge", local / "Microsoft" / "Edge" / "User Data"),
            ("chromium", local / "Chromium" / "User Data"),
        ]

    if sys.platform == "darwin":
        support = Path.home() / "Library" / "Application Support"
        return [
            ("chrome", support / "Google" / "Chrome"),
            ("msedge", support / "Microsoft Edge"),
            ("chromium", support / "Chromium"),
        ]

    config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [
        ("chrome", config_home / "google-chrome"),
        ("msedge", config_home / "microsoft-edge"),
        ("chromium", config_home / "chromium"),
    ]


def _infer_channel_from_user_data_dir(path: Path) -> str:
    target = str(path).lower()
    for channel, candidate in _default_system_profile_candidates():
        if str(candidate).lower() in target:
            return channel
    return ""


def _browser_cache_key(session_key: str, config: Any = None) -> str:
    settings = _browser_settings(config)
    if settings["mode"] == "isolated":
        return session_key

    fingerprint = "|".join(
        [
            settings["mode"],
            settings["channel"],
            settings["cdp_url"],
            settings["user_data_dir"],
            settings["profile_directory"],
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"shared-browser-{digest}"


class BrowserManager:
    """
    Manages a persistent Chrome browser instance for web automation.
    Uses accessibility tree for element identification.
    """

    BASE_DIR = Path.home() / ".limebot" / "browser"
    PROFILES_DIR = BASE_DIR / "profiles"
    SCREENSHOTS_DIR = BASE_DIR / "screenshots"
    DOWNLOADS_DIR = Path.cwd() / "temp" / "downloads" / "browser"
    SNAPSHOT_SKIP_NAMES = frozenset(
        {
            "cache",
            "code cache",
            "gpucache",
            "dawncache",
            "grshadercache",
            "shadercache",
            "media cache",
            "crashpad",
            "crash reports",
            "safe browsing",
            "optimizationguidepredictionmodels",
            "service worker",
            "blob_storage",
        }
    )

    _LB_ATTR = "data-lb-id"

    def __init__(
        self,
        session_key: str,
        headless: bool = False,
        config: Any = None,
        manager_key: Optional[str] = None,
    ):
        self.session_key = session_key
        self.headless = headless
        self.config = config
        self.manager_key = manager_key or session_key
        self.settings = _browser_settings(config)
        self.mode = self.settings["mode"]
        self.channel = self.settings["channel"]
        self.cdp_url = self.settings["cdp_url"]
        self.profile_directory = self.settings["profile_directory"]
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._element_map: Dict[str, Any] = {}
        self._attached_browser = False
        self._action_lock = asyncio.Lock()
        self._system_source_user_data_dir: Optional[Path] = None
        self._system_snapshot_dir: Optional[Path] = None
        self._using_system_snapshot = False

        storage_key = session_key if self.mode == "isolated" else self.manager_key
        storage_name = self._storage_name(storage_key)
        self.user_data_dir: Optional[Path] = None
        self.screenshots_dir = self.SCREENSHOTS_DIR / storage_name
        self.downloads_dir = self.DOWNLOADS_DIR / storage_name

        if self.mode in {"isolated", "shared"}:
            self.user_data_dir = self.PROFILES_DIR / storage_name / "user-data"
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
        elif self.mode == "system":
            self.user_data_dir, detected_channel = self._resolve_system_profile()
            self._system_source_user_data_dir = self.user_data_dir
            if not self.channel and detected_channel:
                self.channel = detected_channel

        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self._browser_lock = asyncio.Lock()

    @staticmethod
    def _storage_name(session_key: str) -> str:
        """Build a stable filesystem-safe folder name for a browser session."""
        normalized = (session_key or "default").strip()
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-") or "default"
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"{safe[:40]}-{digest}"

    def _resolve_system_profile(self) -> tuple[Path, str]:
        configured_dir = self.settings["user_data_dir"]
        if configured_dir:
            resolved = Path(configured_dir).expanduser()
            if not resolved.exists():
                raise RuntimeError(
                    f"Configured browser profile directory does not exist: {resolved}"
                )
            return resolved, self.channel or _infer_channel_from_user_data_dir(resolved)

        candidates = []
        for candidate_channel, candidate_dir in _default_system_profile_candidates():
            if self.channel and candidate_channel != self.channel:
                continue
            if candidate_dir.exists():
                candidates.append((candidate_channel, candidate_dir))

        if not candidates:
            if self.channel:
                raise RuntimeError(
                    f"No browser profile found for channel '{self.channel}'. "
                    "Set BROWSER_USER_DATA_DIR explicitly."
                )
            raise RuntimeError(
                "No supported system browser profile was detected. "
                "Set BROWSER_CHANNEL or BROWSER_USER_DATA_DIR explicitly."
            )

        candidates.sort(
            key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0,
            reverse=True,
        )
        chosen_channel, chosen_dir = candidates[0]
        logger.info(
            f"Using system browser profile '{chosen_channel}' from {chosen_dir}"
        )
        return chosen_dir, chosen_channel

    def _launch_args(self) -> List[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        if self.profile_directory:
            args.append(f"--profile-directory={self.profile_directory}")
        return args

    def _launch_kwargs(self, viewport: Dict[str, int]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "headless": self.headless,
            "viewport": viewport,
            "args": self._launch_args(),
        }
        if self.channel:
            kwargs["channel"] = self.channel
        return kwargs

    @staticmethod
    def _is_profile_locked_error(error: Exception) -> bool:
        text = str(error)
        return any(
            marker in text
            for marker in (
                "ProcessSingleton",
                "profile directory is already in use",
                "user data directory is already in use",
                "SingletonLock",
            )
        )

    def _format_system_profile_error(self, error: Exception) -> str:
        source_dir = self._system_source_user_data_dir or self.user_data_dir
        base = f"Failed to open the system browser profile at {source_dir}."
        if self._is_profile_locked_error(error):
            return (
                f"{base} That profile is already in use by your browser. "
                "Close that browser first, or switch to BROWSER_MODE=attach "
                "and point BROWSER_CDP_URL at a live Chrome/Edge debugging endpoint."
            )
        return f"{base} {error}"

    @classmethod
    def _should_skip_snapshot_name(cls, name: str) -> bool:
        lowered = name.strip().lower()
        if lowered in cls.SNAPSHOT_SKIP_NAMES:
            return True
        if lowered.startswith("singleton"):
            return True
        return lowered.endswith((".lock", ".tmp"))

    @staticmethod
    def _is_browser_profile_dir(name: str) -> bool:
        return (
            name == "Default"
            or name.startswith("Profile ")
            or name in {"Guest Profile", "System Profile"}
        )

    def _copy_tree_soft(self, source: Path, destination: Path) -> None:
        for root, dirs, files in os.walk(source):
            root_path = Path(root)
            relative = root_path.relative_to(source)
            target_root = destination / relative
            target_root.mkdir(parents=True, exist_ok=True)

            dirs[:] = [
                directory
                for directory in dirs
                if not self._should_skip_snapshot_name(directory)
            ]

            for filename in files:
                if self._should_skip_snapshot_name(filename):
                    continue
                source_file = root_path / filename
                target_file = target_root / filename
                try:
                    shutil.copy2(source_file, target_file)
                except OSError as copy_error:
                    logger.debug(
                        f"Skipping locked system-profile file during snapshot: {source_file} ({copy_error})"
                    )

    def _copy_system_profile_snapshot(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        selected_profile = (self.profile_directory or "").strip()

        for entry in source.iterdir():
            if self._should_skip_snapshot_name(entry.name):
                continue

            if entry.is_dir():
                if selected_profile:
                    if entry.name not in {selected_profile, "System Profile"}:
                        continue
                elif not self._is_browser_profile_dir(entry.name):
                    continue

                self._copy_tree_soft(entry, destination / entry.name)
                continue

            try:
                shutil.copy2(entry, destination / entry.name)
            except OSError as copy_error:
                logger.debug(
                    f"Skipping locked system-profile file during snapshot: {entry} ({copy_error})"
                )

    def _prepare_system_profile_snapshot(self) -> Path:
        source = self._system_source_user_data_dir
        if source is None:
            raise RuntimeError("System browser mode did not resolve a source profile.")

        snapshot_dir = (
            self.PROFILES_DIR / self._storage_name(self.manager_key) / "system-snapshot"
        )
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir, ignore_errors=True)

        self._copy_system_profile_snapshot(source, snapshot_dir)
        self._system_snapshot_dir = snapshot_dir
        self._using_system_snapshot = True
        return snapshot_dir

    def _resolve_download_dest(self, filename: str = "", dest: str = "") -> Path:
        """Keep downloads inside temp/ or an explicit allowlisted dest."""
        requested_name = Path(str(filename or "")).name
        raw_dest = str(dest or "").strip()
        if raw_dest:
            target = Path(raw_dest).expanduser()
            if not target.is_absolute():
                target = (Path.cwd() / target).resolve()
            else:
                target = target.resolve()
            if target.suffix or requested_name:
                if target.exists() and target.is_dir():
                    target = target / (requested_name or f"download_{int(time.time())}")
            else:
                target = target / (requested_name or f"download_{int(time.time())}")
        else:
            safe_name = requested_name or f"download_{int(time.time())}"
            target = (self.downloads_dir / safe_name).resolve()

        roots = [
            self.DOWNLOADS_DIR.resolve(),
            (Path.cwd() / "temp").resolve(),
        ]
        state_dir = str(os.environ.get("LIMEBOT_STATE_DIR") or "").strip()
        if state_dir:
            roots.append(Path(state_dir).resolve())
        if not any(target == root or root in target.parents for root in roots):
            raise ValueError(
                f"Download dest must stay under temp/ or LIMEBOT_STATE_DIR: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    async def _ensure_browser(self) -> Page:
        """Ensure browser is running and return the active page."""
        if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
            raise RuntimeError(BROWSER_INSTALL_HINT)
        async with self._browser_lock:
            if self._browser is not None and not self._has_live_browser_connection():
                logger.warning("Browser connection was lost. Reconnecting...")
                await self._do_close()

            if self._page is not None:
                if not self._page.is_closed():
                    return self._page
                logger.warning("Active page was closed. Recovering...")
                if self._context and self._context.pages:
                    self._page = self._context.pages[-1]
                    return self._page
                await self._do_close()

            logger.info(
                f"Launching browser (mode={self.mode}, channel={self.channel or 'default'})..."
            )
            self._playwright = await async_playwright().start()
            viewport = {"width": 1280, "height": 720}

            if self.mode == "attach":
                try:
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        self.cdp_url
                    )
                    self._attached_browser = True
                    if not self._browser.contexts:
                        raise RuntimeError(
                            "Attached browser did not expose a default context."
                        )
                    self._context = self._browser.contexts[0]
                except Exception as attach_error:
                    await self._playwright.stop()
                    self._playwright = None
                    raise RuntimeError(
                        "Failed to attach to an existing browser session. "
                        f"Tried {self.cdp_url}. Launch Chrome or Edge with "
                        "--remote-debugging-port=9222 or set BROWSER_CDP_URL explicitly. "
                        f"Original error: {attach_error}"
                    ) from attach_error
            else:
                try:
                    if self.user_data_dir is None:
                        raise RuntimeError(
                            "Browser mode requires a user data directory, but none was resolved."
                        )
                    self._context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.user_data_dir),
                        **self._launch_kwargs(viewport),
                    )
                except Exception as persistent_error:
                    if self.mode == "system":
                        try:
                            snapshot_dir = await asyncio.to_thread(
                                self._prepare_system_profile_snapshot
                            )
                            self.user_data_dir = snapshot_dir
                            logger.info(
                                f"Falling back to a snapshot of the system browser profile: {snapshot_dir}"
                            )
                            self._context = await self._playwright.chromium.launch_persistent_context(
                                user_data_dir=str(snapshot_dir),
                                **self._launch_kwargs(viewport),
                            )
                        except Exception as snapshot_error:
                            await self._playwright.stop()
                            self._playwright = None
                            raise RuntimeError(
                                f"{self._format_system_profile_error(persistent_error)} "
                                f"Snapshot retry also failed: {snapshot_error}"
                            ) from snapshot_error

                    if self._context is None:
                        logger.warning(
                            f"Persistent browser launch failed ({persistent_error}). "
                            "Falling back to ephemeral context."
                        )
                        try:
                            self._browser = await self._playwright.chromium.launch(
                                headless=self.headless,
                                channel=self.channel or None,
                                args=self._launch_args(),
                            )
                            self._context = await self._browser.new_context(
                                viewport=viewport
                            )
                        except Exception as fallback_error:
                            await self._playwright.stop()
                            self._playwright = None
                            raise RuntimeError(
                                "Failed to start browser in both persistent and ephemeral modes. "
                                f"Persistent error: {persistent_error} | "
                                f"Fallback error: {fallback_error}"
                            ) from fallback_error

            self._context.on("page", self._handle_new_page)

            self._page = (
                self._context.pages[-1]
                if self._context.pages
                else await self._context.new_page()
            )

            await self._page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )

            logger.info("Browser launched successfully")

            # ── Session Tracking ──────────────────────────────────────────────
            from core.browser_sessions import get_browser_session_manager
            manager = get_browser_session_manager()
            self._profile_id = await manager.register_session(
                session_key=self.session_key,
                mode=self.mode,
                metadata={
                    "channel": self.channel,
                    "cdp_url": self.cdp_url if self.mode == "attach" else None,
                    "headless": self.headless,
                }
            )
            return self._page

    def _has_live_browser_connection(self) -> bool:
        """Best-effort health check for the active browser transport."""
        if self._browser is None:
            return True

        is_connected = getattr(self._browser, "is_connected", None)
        if not callable(is_connected):
            return True

        try:
            return bool(is_connected())
        except Exception as health_error:
            logger.debug(f"Browser connection health check failed: {health_error}")
            return False
    async def _handle_new_page(self, page: Page) -> None:
        """Switch focus to newly opened tabs."""
        logger.info(f"New tab detected: {page.url}. Switching focus.")
        self._page = page
        self._element_map.clear()

    @staticmethod
    def _is_navigation_handoff_error(error: Exception) -> bool:
        """Detect aborted navigations that often occur when a site opens a new tab."""
        text = str(error)
        return any(
            marker in text
            for marker in (
                "net::ERR_ABORTED",
                "NS_BINDING_ABORTED",
            )
        )

    @staticmethod
    def _urls_seem_related(requested_url: str, actual_url: str) -> bool:
        if not requested_url or not actual_url:
            return False

        requested = urllib.parse.urlsplit(requested_url)
        actual = urllib.parse.urlsplit(actual_url)
        if requested.netloc.lower() != actual.netloc.lower():
            return False

        requested_path = requested.path.rstrip("/")
        actual_path = actual.path.rstrip("/")
        if requested_path == actual_path:
            return True
        return (
            actual_path.startswith(requested_path)
            or requested_path.startswith(actual_path)
        )

    async def _recover_navigation_handoff(
        self, requested_url: str, existing_page_ids: set[int]
    ) -> Optional[Page]:
        """Follow a new tab/page when the original goto is aborted mid-handoff."""
        if not self._context:
            return None

        deadline = time.monotonic() + 5.0
        fallback: Optional[Page] = None

        while time.monotonic() < deadline:
            open_pages = [
                candidate
                for candidate in self._context.pages
                if not candidate.is_closed()
            ]

            new_pages = [
                candidate
                for candidate in open_pages
                if id(candidate) not in existing_page_ids
            ]

            for candidate in new_pages:
                current_url = candidate.url or ""
                if not current_url or current_url == "about:blank":
                    continue
                if self._urls_seem_related(requested_url, current_url):
                    return candidate
                if fallback is None:
                    fallback = candidate

            current = self._page
            if current and not current.is_closed():
                current_url = current.url or ""
                if current_url and current_url != "about:blank":
                    if self._urls_seem_related(requested_url, current_url):
                        return current
                    if id(current) not in existing_page_ids and fallback is None:
                        fallback = current

            await asyncio.sleep(0.1)

        return fallback

    @staticmethod
    def _detect_page_warning(title: str, url: str, elements: str) -> Optional[str]:
        """Classify common access walls so the model can react explicitly."""
        title_text = (title or "").lower()
        url_text = (url or "").lower()
        elements_text = (elements or "").lower()
        combined = "\n".join([title_text, elements_text])

        if any(
            marker in combined
            for marker in (
                "verifying your browser",
                "verify you are human",
                "checking your browser",
                "captcha",
            )
        ):
            return "Site is showing a browser verification page instead of the requested content."

        if "x.com" in url_text or "twitter.com" in url_text:
            if any(
                marker in combined
                for marker in (
                    "iniciar sesión",
                    "log in",
                    "sign up",
                    "sign in to x",
                    "create account",
                    "regístrate",
                )
            ):
                return "X is showing the login wall instead of the post content."

            if any(
                marker in combined
                for marker in (
                    "something went wrong",
                    "try reloading",
                    "unusual activity",
                    "suspicious activity",
                    "verify you are human",
                    "browser verification",
                )
            ):
                return "X is showing an interstitial or anti-bot page instead of the post content."

        return None

    async def _finalize_navigation(
        self, page: Page, on_progress=None, note: str = "", recovered: bool = False
    ) -> Dict[str, Any]:
        """Collect the final page state after a navigation or handoff."""
        self._page = page

        try:
            await page.bring_to_front()
        except Exception:
            pass

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        if on_progress:
            await on_progress("⏳ Waiting for page to settle...")
        await page.wait_for_timeout(2000)

        if on_progress:
            await on_progress("🛡️ Handling overlays...")
        await self._handle_overlays()

        title = await page.title()
        current_url = page.url
        if on_progress:
            await on_progress("🌳 Building accessibility tree...")
        a11y_tree = await self._build_accessibility_tree()

        if on_progress:
            await on_progress("✅ Navigation complete.")

        result = {
            "success": True,
            "title": title,
            "url": current_url,
            "elements": a11y_tree,
        }
        warning = self._detect_page_warning(title, current_url, a11y_tree)
        if note:
            result["note"] = note
        if warning:
            result["warning"] = warning
        if recovered:
            result["recovered"] = True
        return result

    async def close(self) -> None:
        """Close the browser instance."""
        async with self._action_lock:
            async with self._browser_lock:
                await self._do_close()

    async def _do_close(self) -> None:
        """Internal close — call only when lock is already held."""
        if self._context and not self._attached_browser:
            await self._context.close()
        self._context = None
        self._page = None

        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        if self._system_snapshot_dir and self._system_snapshot_dir.exists():
            await asyncio.to_thread(
                shutil.rmtree, self._system_snapshot_dir, True
            )
        if self.mode == "system" and self._system_source_user_data_dir is not None:
            self.user_data_dir = self._system_source_user_data_dir
        self._system_snapshot_dir = None
        self._using_system_snapshot = False
        self._attached_browser = False
        self._element_map.clear()
        
        # ── Session Tracking ──────────────────────────────────────────────
        if getattr(self, "_profile_id", None):
            from core.browser_sessions import get_browser_session_manager
            manager = get_browser_session_manager()
            await manager.mark_closed(self._profile_id)
        
        logger.info("Browser closed")

    async def list_tabs(self) -> List[Dict[str, Any]]:
        """List all open tabs."""
        async with self._action_lock:
            if not self._context:
                return []
            tabs = []
            for i, p in enumerate(self._context.pages):
                try:
                    tabs.append(
                        {
                            "index": i,
                            "title": await p.title(),
                            "url": p.url,
                            "active": p == self._page,
                        }
                    )
                except Exception:
                    continue
            return tabs

    async def switch_tab(self, index: int) -> bool:
        """Switch to a specific tab by index."""
        async with self._action_lock:
            if not self._context or index >= len(self._context.pages):
                return False
            self._page = self._context.pages[index]
            await self._page.bring_to_front()
            self._element_map.clear()
            return True

    async def _handle_overlays(self) -> None:
        """Dismiss common cookie banners or overlays that may block interactions."""
        if not self._page:
            return

        accept_selectors = [
            "button:has-text('Accept')",
            "button:has-text('Aceptar')",
            "button:has-text('Agree')",
            "button:has-text('Consent')",
            "#onetrust-accept-btn-handler",
            ".cookie-banner-close",
        ]
        close_selectors = [
            "button:has-text('Close')",
            "button:has-text('Cerrar')",
            "[aria-label='Close']",
            ".modal-close",
        ]

        for selector in accept_selectors:
            try:
                btn = self._page.locator(selector).first
                if await btn.is_visible(timeout=500):
                    logger.info(f"Auto-dismissing overlay: {selector}")
                    await btn.click()
                    await self._page.wait_for_timeout(500)
            except Exception:
                continue

        for selector in close_selectors:
            try:
                btn = self._page.locator(selector).first
                if await btn.is_visible(timeout=500):
                    has_inputs = await btn.evaluate("""
                        (el) => {
                            const parent = el.closest('div, section, dialog, [role="dialog"]');
                            return parent ? parent.querySelectorAll('input, textbox').length > 0 : false;
                        }
                    """)
                    if not has_inputs:
                        logger.info(f"Dismissing non-form overlay: {selector}")
                        await btn.click()
                        await self._page.wait_for_timeout(500)
            except Exception:
                continue

    _JS_COLLECT = """
    (args) => {
        const { startId, framePrefix, attrName } = args;
        let currentId = startId;
        const items = [];

        function isVisible(elem) {
            if (!elem) return false;
            const style = window.getComputedStyle(elem);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = elem.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        }

        function walk(root) {
            const candidates = root.querySelectorAll(
                'a, button, input, select, textarea, [role="button"], [role="link"], ' +
                '[role="textbox"], [role="checkbox"], [role="radio"], [onclick], ' +
                '[tabindex]:not([tabindex="-1"])'
            );

            for (const elem of candidates) {
                if (!isVisible(elem)) continue;

                const idVal = framePrefix + "-" + currentId;
                elem.setAttribute(attrName, idVal);

                let text = (elem.innerText || elem.getAttribute("aria-label") ||
                            elem.getAttribute("placeholder") || elem.value || "").trim().substring(0, 50);
                const tag  = elem.tagName.toLowerCase();
                const role = elem.getAttribute("role") || tag;

                items.push({ lb_id: "e" + currentId, unique_id: idVal, text, tag, role });
                currentId++;
            }

            // Recurse into shadow roots
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        }

        walk(document);
        return { items, nextId: currentId };
    }
    """

    _ELEMENT_TYPE_MAP = {
        "a": "link",
        "button": "button",
        "input": "input",
        "select": "dropdown",
        "textarea": "textbox",
        "checkbox": "checkbox",
        "radio": "radio",
    }

    def _get_element_type(self, tag: str, role: str) -> str:
        return self._ELEMENT_TYPE_MAP.get(tag, role)

    async def _build_accessibility_tree(self) -> str:
        """Build a text representation of the page's accessibility tree across all frames."""
        page = await self._ensure_browser()
        self._element_map.clear()

        tree_lines: List[str] = []

        counter = {"id": 1}

        async def process_frame(frame, frame_index: int) -> None:
            try:
                result = await frame.evaluate(
                    self._JS_COLLECT,
                    {
                        "startId": counter["id"],
                        "framePrefix": f"f{frame_index}",
                        "attrName": self._LB_ATTR,
                    },
                )
            except Exception as e:
                logger.debug(f"Frame {frame_index} skipped: {e}")
                return

            items = result.get("items", [])
            counter["id"] = result.get("nextId", counter["id"])

            is_main = frame == page.main_frame
            for item in items:
                eid = item["lb_id"]
                unique_id = item["unique_id"]
                text = item["text"]
                role = item["role"]
                tag = item["tag"]

                self._element_map[eid] = frame.locator(
                    f'[{self._LB_ATTR}="{unique_id}"]'
                )

                type_desc = self._get_element_type(tag, role)
                text_display = f'"{text}"' if text else "(no label)"
                frame_info = "" if is_main else f" [Frame: {frame.name or 'anon'}]"
                tree_lines.append(f"[{eid}]{frame_info} {text_display} ({type_desc})")

        await process_frame(page.main_frame, 0)
        for i, child in enumerate(page.frames):
            if child != page.main_frame:
                await process_frame(child, i + 1)

        return (
            "\n".join(tree_lines) if tree_lines else "(No interactive elements found)"
        )

    async def navigate(self, url: str, on_progress=None) -> Dict[str, Any]:
        """Navigate to a URL and return a page snapshot."""
        async with self._action_lock:
            page = await self._ensure_browser()
            existing_page_ids = (
                {id(candidate) for candidate in self._context.pages}
                if self._context
                else set()
            )

            try:
                if on_progress:
                    await on_progress(f"🌍 Navigating to {url}...")
                logger.info(f"Navigating to: {url}")
                await asyncio.sleep(random.uniform(0.5, 1.5))

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                return await self._finalize_navigation(page, on_progress=on_progress)

            except Exception as e:
                if self._is_navigation_handoff_error(e):
                    recovered_page = await self._recover_navigation_handoff(
                        url, existing_page_ids
                    )
                    if recovered_page is not None:
                        logger.warning(
                            "Recovered navigation after aborted goto by following "
                            f"page handoff: {recovered_page.url}"
                        )
                        note = (
                            "Recovered after the site opened the destination in a new tab."
                        )
                        return await self._finalize_navigation(
                            recovered_page,
                            on_progress=on_progress,
                            note=note,
                            recovered=True,
                        )

                logger.error(f"Navigation failed: {e}")
                return {"success": False, "error": str(e)}

    async def click(self, element_id: str) -> Dict[str, Any]:
        """Click an element by its accessibility ID."""
        async with self._action_lock:
            if element_id not in self._element_map:
                return {
                    "success": False,
                    "error": f"Element '{element_id}' not found. Run snapshot first.",
                }

            try:
                locator = self._element_map[element_id]
                try:
                    await locator.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass

                await asyncio.sleep(random.uniform(0.2, 0.5))
                await locator.click()
                await self._page.wait_for_timeout(1000)
                await self._handle_overlays()

                title = await self._page.title()
                a11y_tree = await self._build_accessibility_tree()

                return {
                    "success": True,
                    "message": f"Clicked {element_id}",
                    "title": title,
                    "elements": a11y_tree,
                }

            except Exception as e:
                logger.error(f"Click failed: {e}")
                return {"success": False, "error": str(e)}

    async def download(
        self,
        element_id: str = "",
        filename: str = "",
        timeout_ms: int = 30_000,
        dest: str = "",
        url: str = "",
    ) -> Dict[str, Any]:
        """Click a known element or open a direct URL, then save the download."""
        if not PLAYWRIGHT_AVAILABLE:
            return {"success": False, "error": BROWSER_INSTALL_HINT}

        async with self._action_lock:
            direct_url = str(url or "").strip()
            element = str(element_id or "").strip()
            if not direct_url and element not in self._element_map:
                return {
                    "success": False,
                    "error": (
                        f"Element '{element_id}' not found. Run snapshot first, "
                        "or pass url= for a direct file link."
                    ),
                }

            try:
                # Large ISOs and installers need minutes, not 30 seconds.
                timeout_ms = max(1_000, min(int(timeout_ms or 30_000), 1_800_000))
                page = await self._ensure_browser()
                if direct_url and not element:
                    async with page.expect_download(timeout=timeout_ms) as pending:
                        await page.goto(direct_url, wait_until="commit")
                else:
                    locator = self._element_map[element]
                    try:
                        await locator.scroll_into_view_if_needed(timeout=2_000)
                    except Exception:
                        pass
                    async with page.expect_download(timeout=timeout_ms) as pending:
                        await locator.click()
                download = await pending.value

                failure = await download.failure()
                if failure:
                    return {"success": False, "error": f"Download failed: {failure}"}

                requested_name = Path(str(filename or "")).name
                suggested_name = Path(download.suggested_filename or "").name
                safe_name = requested_name or suggested_name or f"download_{int(time.time())}"
                safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", safe_name).strip(" .")
                if not safe_name:
                    safe_name = f"download_{int(time.time())}"

                destination = self._resolve_download_dest(safe_name, dest)
                if destination.exists():
                    destination = destination.with_name(
                        f"{destination.stem}_{int(time.time())}{destination.suffix}"
                    )
                await download.save_as(str(destination))

                return {
                    "success": True,
                    "message": f"Downloaded {destination.name}",
                    "path": str(destination.resolve()),
                    "suggested_filename": suggested_name,
                    "url": download.url,
                    "bytes": destination.stat().st_size if destination.exists() else 0,
                }
            except Exception as e:
                logger.error(f"Download failed: {e}")
                return {"success": False, "error": str(e)}

    async def type_text(self, element_id: str, text: str) -> Dict[str, Any]:
        """Type text into an element with human-like typing speed."""
        async with self._action_lock:
            if element_id not in self._element_map:
                return {
                    "success": False,
                    "error": f"Element '{element_id}' not found. Run snapshot first.",
                }

            try:
                locator = self._element_map[element_id]
                await locator.click(timeout=5000)
                await asyncio.sleep(random.uniform(0.3, 0.7))
                await self._page.keyboard.press("Control+A")
                await self._page.keyboard.press("Backspace")
                await locator.type(text, delay=random.randint(50, 150))

                return {"success": True, "message": f"Typed text into {element_id}"}

            except Exception as e:
                logger.error(f"Type failed: {e}")
                return {"success": False, "error": str(e)}

    async def snapshot(self, take_screenshot: bool = True) -> Dict[str, Any]:
        """Get current page state including accessibility tree."""
        async with self._action_lock:
            page = await self._ensure_browser()

            try:
                await self._handle_overlays()

                title = await page.title()
                current_url = page.url
                a11y_tree = await self._build_accessibility_tree()

                result = {
                    "success": True,
                    "title": title,
                    "url": current_url,
                    "elements": a11y_tree,
                }
                warning = self._detect_page_warning(title, current_url, a11y_tree)
                if warning:
                    result["warning"] = warning

                if take_screenshot:
                    screenshot_path = (
                        self.screenshots_dir / f"snapshot_{time.time():.0f}.png"
                    )
                    await page.screenshot(path=str(screenshot_path))
                    result["screenshot"] = str(screenshot_path)

                return result

            except Exception as e:
                logger.error(f"Snapshot failed: {e}")
                return {"success": False, "error": str(e)}

    async def scroll(
        self, direction: str = "down", amount: int = 500
    ) -> Dict[str, Any]:
        """Scroll the page up or down."""
        async with self._action_lock:
            if direction not in ("up", "down"):
                return {
                    "success": False,
                    "error": f"Invalid direction '{direction}'. Use 'up' or 'down'.",
                }

            page = await self._ensure_browser()

            try:
                scroll_amount = amount if direction == "down" else -amount

                await page.evaluate("(amt) => window.scrollBy(0, amt)", scroll_amount)
                await page.wait_for_timeout(300)

                a11y_tree = await self._build_accessibility_tree()
                return {
                    "success": True,
                    "message": f"Scrolled {direction} by {amount}px",
                    "elements": a11y_tree,
                }

            except Exception as e:
                logger.error(f"Scroll failed: {e}")
                return {"success": False, "error": str(e)}

    async def wait(self, ms: int = 1000) -> Dict[str, Any]:
        """Wait for a specified time to let the page update."""
        async with self._action_lock:
            ms = max(100, min(ms, 30_000))
            page = await self._ensure_browser()

            try:
                await page.wait_for_timeout(ms)
                a11y_tree = await self._build_accessibility_tree()
                return {
                    "success": True,
                    "message": f"Waited {ms}ms",
                    "elements": a11y_tree,
                }

            except Exception as e:
                logger.error(f"Wait failed: {e}")
                return {"success": False, "error": str(e)}

    async def extract(
        self, selector: str = "body", limit: int = 5000
    ) -> Dict[str, Any]:
        """
        Extract text content from the page or a specific selector.
        Searches all frames if not found in the main frame.

        FIX: merged extract() and extract_large() into one method with a limit parameter.
        """
        async with self._action_lock:
            page = await self._ensure_browser()

            try:
                elem = await page.query_selector(selector)

                if not elem:
                    for frame in page.frames:
                        if frame == page.main_frame:
                            continue
                        try:
                            elem = await frame.query_selector(selector)
                            if elem:
                                break
                        except Exception:
                            continue

                if not elem:
                    return {
                        "success": False,
                        "error": f"Selector '{selector}' not found in any frame",
                    }

                text = await elem.inner_text()
                original_length = len(text)
                truncated = original_length > limit
                if truncated:
                    text = text[:limit] + f"\n... (truncated at {limit} chars)"

                return {
                    "success": True,
                    "selector": selector,
                    "text": text,
                    "truncated": truncated,
                    "original_length": original_length,
                }

            except Exception as e:
                logger.error(f"Extract failed: {e}")
                return {"success": False, "error": str(e)}

    async def list_media(self, on_progress=None) -> Dict[str, Any]:
        """Extract a list of images from the current page."""
        async with self._action_lock:
            page = await self._ensure_browser()
            url = page.url

            try:
                if on_progress:
                    await on_progress("📸 Scanning page for media...")

                if "google.com" in url and "tbm=isch" in url:
                    if on_progress:
                        await on_progress(
                            "🔍 Detected Google Image results. Extracting thumbnails..."
                        )
                    js_script = """
                    () => {
                        const items = Array.from(document.querySelectorAll('div.isv-r, div.rg_bx'));
                        return items.map(item => {
                            const img   = item.querySelector('img');
                            const title = item.querySelector('h3, .mVD9t');
                            if (!img || !img.src) return null;
                            return { src: img.src, alt: (title ? title.innerText : img.alt) || "Google Image Result",
                                     width: img.width, height: img.height, visible: true };
                        }).filter(Boolean);
                    }
                    """
                else:
                    js_script = """
                    () => {
                        return Array.from(document.querySelectorAll('img')).map(img => {
                            const rect = img.getBoundingClientRect();
                            return { src: img.src, alt: img.alt || img.title || "",
                                     width: rect.width, height: rect.height,
                                     visible: rect.width > 10 && rect.height > 10 };
                        }).filter(img => img.src && img.src.startsWith('http'));
                    }
                    """

                all_imgs = await page.evaluate(js_script)

                seen: set = set()
                filtered = []
                for img in all_imgs:
                    if img["src"] in seen:
                        continue
                    if (
                        img["width"] > 50 or img["height"] > 50 or len(img["alt"]) > 3
                    ) and img["visible"]:
                        filtered.append(img)
                        seen.add(img["src"])

                filtered = filtered[:20]

                if not filtered:
                    return {
                        "success": True,
                        "count": 0,
                        "media_summary": "No significant images found. Try scrolling or navigating deeper.",
                    }

                lines = [f"📸 Found {len(filtered)} images:\n"]
                for i, img in enumerate(filtered, 1):
                    desc = img["alt"].replace("\n", " ").strip() or f"Image {i}"
                    lines.append(f"{i}. {desc}\n   URL: {img['src']}\n")

                if on_progress:
                    await on_progress(f"✅ {len(filtered)} images indexed.")
                return {
                    "success": True,
                    "count": len(filtered),
                    "media_summary": "\n".join(lines),
                }

            except Exception as e:
                logger.error(f"Media extraction failed: {e}")
                return {"success": False, "error": str(e)}

    async def google_search(self, query: str, on_progress=None) -> Dict[str, Any]:
        """Search Google and return structured results."""
        async with self._action_lock:
            page = await self._ensure_browser()

            try:
                if on_progress:
                    await on_progress(f"🔍 Searching Google for: {query}")
                logger.info(f"Google search: {query}")

                encoded_query = urllib.parse.quote_plus(query)
                await page.goto(f"https://www.google.com/search?q={encoded_query}")
                await page.wait_for_timeout(1000)

                if on_progress:
                    await on_progress("📄 Extracting top results...")
                results = []
                seen_urls = set()

                # Google's wrapper classes change frequently. Anchor the parse on
                # the comparatively stable result shape (a link containing an h3)
                # and use the legacy container selector only as a fallback.
                result_links = await page.query_selector_all("a:has(h3)")
                for link_elem in result_links[:12]:
                    title_elem = await link_elem.query_selector("h3")
                    url = await link_elem.get_attribute("href")
                    if not title_elem or not url or not url.startswith(("http://", "https://")):
                        continue
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    title = (await title_elem.inner_text()).strip()
                    if not title:
                        continue
                    snippet = ""
                    try:
                        container = await link_elem.query_selector("xpath=ancestor::div[@data-snhf][1]")
                        if container:
                            text = (await container.inner_text()).strip()
                            snippet = text[len(title) :].strip()[:600]
                    except Exception:
                        snippet = ""
                    results.append({"title": title, "url": url, "snippet": snippet})
                    if len(results) >= 5:
                        break

                if not results:
                    for container in (await page.query_selector_all("div.g"))[:8]:
                        title_elem = await container.query_selector("h3")
                        link_elem = await container.query_selector("a")
                        snippet_elem = await container.query_selector("div.VwiC3b")

                        if title_elem and link_elem:
                            url = await link_elem.get_attribute("href")
                            title = (await title_elem.inner_text()).strip()
                            if not url or not title or url in seen_urls:
                                continue
                            seen_urls.add(url)
                            results.append(
                                {
                                    "title": title,
                                    "url": url,
                                    "snippet": await snippet_elem.inner_text()
                                    if snippet_elem
                                    else "",
                                }
                            )

                if not results:
                    text = await page.inner_text("body")
                    explicit_no_results = "No results found" in text
                    return {
                        "success": False,
                        "results": [],
                        "error": (
                            "Google returned no results."
                            if explicit_no_results
                            else "Google results were present but could not be parsed."
                        ),
                    }

                result_text = "".join(
                    f"{i}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}\n\n"
                    for i, r in enumerate(results, 1)
                )

                if on_progress:
                    await on_progress("🌳 Building accessibility tree...")
                a11y_tree = await self._build_accessibility_tree()

                if on_progress:
                    await on_progress(f"✅ Found {len(results)} results.")
                return {
                    "success": True,
                    "query": query,
                    "results": results,
                    "results_summary": result_text,
                    "elements": a11y_tree,
                }

            except Exception as e:
                logger.error(f"Google search failed: {e}")
                return {"success": False, "error": str(e)}

    async def press_key(self, key: str) -> Dict[str, Any]:
        """Press a keyboard key (e.g. 'Enter', 'Escape', 'Tab')."""
        async with self._action_lock:
            page = await self._ensure_browser()
            try:
                await page.keyboard.press(key)
                await page.wait_for_timeout(500)
                a11y_tree = await self._build_accessibility_tree()
                return {
                    "success": True,
                    "message": f"Pressed '{key}'",
                    "elements": a11y_tree,
                }
            except Exception as e:
                logger.error(f"Key press failed: {e}")
                return {"success": False, "error": str(e)}

    async def go_back(self) -> Dict[str, Any]:
        """Navigate to the previous page in history."""
        async with self._action_lock:
            page = await self._ensure_browser()
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1000)
                a11y_tree = await self._build_accessibility_tree()
                return {
                    "success": True,
                    "title": await page.title(),
                    "url": page.url,
                    "elements": a11y_tree,
                }
            except Exception as e:
                logger.error(f"go_back failed: {e}")
                return {"success": False, "error": str(e)}

    async def get_page_text(self) -> Dict[str, Any]:
        """Return all visible text on the current page — useful for reading articles."""
        return await self.extract("body", limit=20_000)


_browser_managers: Dict[str, BrowserManager] = {}

_singleton_lock = asyncio.Lock()


async def get_browser_manager(
    session_key: str = "default", headless: bool = False, config: Any = None
) -> BrowserManager:
    """Get or create the BrowserManager for a specific agent session."""
    cache_key = _browser_cache_key(session_key, config)
    async with _singleton_lock:
        manager = _browser_managers.get(cache_key)
        if manager is None:
            manager = BrowserManager(
                session_key=session_key,
                headless=headless,
                config=config,
                manager_key=cache_key,
            )
            _browser_managers[cache_key] = manager
    return manager


async def close_browser(session_key: Optional[str] = None, config: Any = None) -> None:
    """Close one browser session, or all browser sessions when no key is given."""
    managers: List[BrowserManager] = []
    async with _singleton_lock:
        if session_key is None:
            managers = list(_browser_managers.values())
            _browser_managers.clear()
        else:
            keys_to_close = []

            direct_key = _browser_cache_key(session_key, config) if config else session_key
            if direct_key in _browser_managers:
                keys_to_close.append(direct_key)
            else:
                for key, manager in _browser_managers.items():
                    if manager.session_key == session_key:
                        keys_to_close.append(key)

            for key in keys_to_close:
                manager = _browser_managers.pop(key, None)
                if manager:
                    managers.append(manager)

    for manager in managers:
        await manager.close()



