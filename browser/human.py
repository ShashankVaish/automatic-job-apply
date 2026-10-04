"""Human-paced delays, attention sounds, highlighting and screenshots."""
from __future__ import annotations

import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def jitter(min_s: float, max_s: float) -> float:
    """Sleep a random human-ish amount and return how long we slept."""
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)
    return delay


def beep() -> None:
    """Get the user's attention. Falls back to the terminal bell."""
    try:
        if sys.platform == "win32":
            import winsound  # noqa: PLC0415

            for freq in (880, 1175, 1480):
                winsound.Beep(freq, 140)
            return
    except Exception:  # pragma: no cover - audio is best effort
        pass
    sys.stdout.write("\a")
    sys.stdout.flush()


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:limit] or "unknown").strip("-")


def screenshot(page, directory: str | Path, label: str) -> str:
    """Full-page screenshot. Returns the path, or "" if it failed."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = d / "{0}_{1}.png".format(stamp, slugify(label))
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:
        log.debug("screenshot failed for %s: %s", label, exc)
        try:
            page.screenshot(path=str(path))  # viewport-only fallback
            return str(path)
        except Exception:
            return ""


HIGHLIGHT_JS = """
(el) => {
  if (!el) return false;
  el.scrollIntoView({behavior: 'smooth', block: 'center'});
  el.style.outline = '4px solid #ff2d55';
  el.style.outlineOffset = '3px';
  el.style.boxShadow = '0 0 0 9999px rgba(255,45,85,0.10)';
  let on = true;
  const id = setInterval(() => {
    on = !on;
    el.style.outlineColor = on ? '#ff2d55' : '#ffd400';
  }, 450);
  setTimeout(() => clearInterval(id), 30000);
  return true;
}
"""


def highlight(locator) -> bool:
    """Scroll an element into view and make it pulse so it's unmissable."""
    try:
        return bool(locator.evaluate(HIGHLIGHT_JS))
    except Exception as exc:
        log.debug("highlight failed: %s", exc)
        return False


class Pacer:
    """Applies the configured delays, and keeps them out of dry runs."""

    def __init__(
        self,
        action_min: float,
        action_max: float,
        app_min: float,
        app_max: float,
        enabled: bool = True,
    ) -> None:
        self.action_min = action_min
        self.action_max = action_max
        self.app_min = app_min
        self.app_max = app_max
        self.enabled = enabled

    def action(self) -> None:
        if self.enabled:
            jitter(self.action_min, self.action_max)

    def between_applications(self) -> None:
        if not self.enabled:
            return
        delay = random.uniform(self.app_min, self.app_max)
        log.info("Pausing %.0fs before the next application", delay)
        time.sleep(delay)
