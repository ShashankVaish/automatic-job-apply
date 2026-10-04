"""Browser startup. Always headed, so you can watch and take over.

Two modes, chosen in config.yaml:

  persistent  Playwright owns a Chromium profile in ./browser_profile. Simplest;
              log in once and the session is reused.
  cdp         Attach to a Chrome you started yourself with start_chrome.bat /
              start_chrome.sh (--remote-debugging-port=9222). Useful when you
              want your real Chrome, extensions and existing logins.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

from config import Config

log = logging.getLogger(__name__)

# Trim the most obvious automation tell. This is not stealth tooling; it just
# stops sites breaking when they probe for navigator.webdriver.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
]


class BrowserSession:
    """Owns the Playwright lifecycle and hands out pages."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.mode = cfg.browser.mode

    # ----------------------------------------------------------- lifecycle

    def start(self) -> BrowserContext:
        self._pw = sync_playwright().start()
        if self.mode == "cdp":
            self.context = self._connect_cdp(self._pw)
        else:
            self.context = self._launch_persistent(self._pw)

        self.context.set_default_navigation_timeout(self.cfg.browser.nav_timeout_ms)
        self.context.set_default_timeout(self.cfg.browser.nav_timeout_ms)
        try:
            self.context.add_init_script(STEALTH_JS)
        except Exception as exc:  # pragma: no cover
            log.debug("could not add init script: %s", exc)
        return self.context

    def _launch_persistent(self, pw: Playwright) -> BrowserContext:
        profile_dir = self.cfg.abs_path(self.cfg.browser.persistent_profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        log.info("Launching Chromium with persistent profile at %s", profile_dir)
        return pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            slow_mo=self.cfg.browser.slow_mo_ms,
            args=LAUNCH_ARGS,
            viewport=None,
            accept_downloads=True,
        )

    def _connect_cdp(self, pw: Playwright) -> BrowserContext:
        url = self.cfg.browser.cdp_url
        log.info("Connecting to Chrome over CDP at %s", url)
        try:
            self._browser = pw.chromium.connect_over_cdp(url)
        except Exception as exc:
            raise SystemExit(
                "Could not reach Chrome at {0}.\n".format(url)
                + "Start it first:\n"
                "  Windows:  start_chrome.bat\n"
                "  Mac/Linux: ./start_chrome.sh\n"
                "Then re-run this command.\nUnderlying error: {0}".format(exc)
            ) from exc
        if self._browser.contexts:
            return self._browser.contexts[0]
        return self._browser.new_context(accept_downloads=True)

    def new_page(self) -> Any:
        if self.context is None:
            raise RuntimeError("BrowserSession.start() was never called")
        pages = [p for p in self.context.pages if not p.is_closed()]
        # Reuse the blank starter tab rather than piling up windows.
        for page in pages:
            if page.url in ("about:blank", "chrome://newtab/", ""):
                return page
        return self.context.new_page()

    def close(self) -> None:
        """In CDP mode we never close the user's own Chrome."""
        try:
            if self.mode == "cdp":
                if self._browser is not None:
                    self._browser.close()
            elif self.context is not None:
                self.context.close()
        except Exception as exc:  # pragma: no cover
            log.debug("error closing browser: %s", exc)
        finally:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._pw = None
            self._browser = None
            self.context = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def profile_exists(cfg: Config) -> bool:
    d: Path = cfg.abs_path(cfg.browser.persistent_profile_dir)
    return d.is_dir() and any(d.iterdir())
