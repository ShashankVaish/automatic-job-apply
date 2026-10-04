"""Run window, per-site daily caps, and the overall run cap."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import Config
from db import Database

log = logging.getLogger(__name__)


def now_in_window(cfg: Config) -> tuple[bool, str]:
    """Is it an acceptable hour to be applying? Returns (ok, explanation)."""
    win = cfg.limits.run_window
    try:
        tz = ZoneInfo(win.timezone)
    except Exception:
        log.warning("Unknown timezone %r; falling back to local time", win.timezone)
        tz = None

    now = datetime.now(tz) if tz else datetime.now()
    label = now.strftime("%H:%M")

    if not win.enforce:
        return True, "run window not enforced (now {0})".format(label)

    if win.start_hour <= now.hour < win.end_hour:
        return True, "{0} {1} is inside {2:02d}:00-{3:02d}:00".format(
            label, win.timezone, win.start_hour, win.end_hour
        )
    return False, (
        "It is {0} {1}. This assistant only runs between {2:02d}:00 and {3:02d}:00. "
        "Set limits.run_window.enforce: false in config.yaml to override.".format(
            label, win.timezone, win.start_hour, win.end_hour
        )
    )


class RunBudget:
    """Tracks what's left to spend: per-site caps, career-page cap, run cap."""

    def __init__(self, cfg: Config, db: Database, run_max: int = 0) -> None:
        self.cfg = cfg
        self.db = db
        self.run_max = run_max or 0
        self.used_this_run = 0

    # ------------------------------------------------------------- run cap

    def run_cap_reached(self) -> bool:
        return bool(self.run_max) and self.used_this_run >= self.run_max

    def spend(self) -> None:
        """Count one application attempt that reached a form."""
        self.used_this_run += 1

    def remaining_this_run(self) -> str:
        if not self.run_max:
            return "unlimited"
        return "{0}/{1}".format(self.used_this_run, self.run_max)

    # ---------------------------------------------------------- site caps

    def site_cap_reached(self, site: str) -> tuple[bool, str]:
        cap = self.cfg.limits.daily_cap(site)
        used = self.db.count_today(site)
        if used >= cap:
            return True, "{0} daily limit reached ({1}/{2})".format(site, used, cap)
        return False, "{0} {1}/{2} today".format(site, used, cap)

    def career_page_cap_reached(self) -> tuple[bool, str]:
        cap = self.cfg.limits.daily_cap("career_pages")
        used = self.db.count_today_career_pages()
        if used >= cap:
            return True, "career_pages daily limit reached ({0}/{1})".format(used, cap)
        return False, "career_pages {0}/{1} today".format(used, cap)

    # -------------------------------------------------------------- blocks

    def site_blocked(self, site: str) -> str | None:
        return self.db.is_blocked(site)

    def usable_sites(self, sites: list[str]) -> tuple[list[str], dict[str, str]]:
        """Split the requested sites into usable and unusable-with-a-reason."""
        ok: list[str] = []
        rejected: dict[str, str] = {}
        for site in sites:
            blocked = self.site_blocked(site)
            if blocked:
                rejected[site] = "blocked earlier today: " + blocked
                continue
            capped, detail = self.site_cap_reached(site)
            if capped:
                rejected[site] = detail
                continue
            ok.append(site)
        return ok, rejected
