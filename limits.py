"""Run window, per-site daily caps, and the overall run cap."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import Config
from db import Database

log = logging.getLogger(__name__)


def resolve_timezone(name: str) -> tuple[Any, str]:
    """Load a timezone, or say clearly why we could not.

    Windows ships no system timezone database, so without the `tzdata` package
    zoneinfo resolves nothing at all and every window silently becomes local
    time. That is a correctness problem, not a cosmetic one, so the caller gets
    a warning string it can show the user.
    """
    try:
        return ZoneInfo(name), ""
    except Exception as exc:
        warning = (
            "Could not load timezone {0!r} ({1}), so local time is being used "
            "instead. Install the timezone database with "
            "`pip install tzdata` and re-run.".format(name, type(exc).__name__)
        )
        log.warning(warning)
        return None, warning


def now_in_window(cfg: Config) -> tuple[bool, str]:
    """Is it an acceptable hour to be applying? Returns (ok, explanation)."""
    win = cfg.limits.run_window
    tz, tz_warning = resolve_timezone(win.timezone)

    now = datetime.now(tz) if tz else datetime.now()
    label = now.strftime("%H:%M")

    suffix = "  [" + tz_warning + "]" if tz_warning else ""

    if not win.enforce:
        return True, "run window not enforced (now {0}){1}".format(label, suffix)

    if win.start_hour <= now.hour < win.end_hour:
        return True, "{0} {1} is inside {2:02d}:00-{3:02d}:00{4}".format(
            label, win.timezone, win.start_hour, win.end_hour, suffix
        )
    return False, (
        "It is {0} {1}. This assistant only runs between {2:02d}:00 and {3:02d}:00. "
        "Set limits.run_window.enforce: false in config.yaml to override.{4}".format(
            label, win.timezone, win.start_hour, win.end_hour, suffix
        )
    )


def email_window_now(cfg: Config) -> tuple[bool, str]:
    """Is it an acceptable moment to send cold email?

    Weekdays only, inside the configured window. Cold email at 2am on a Sunday
    reads as a bot, so this is a hard gate on sending - queueing and drafting
    are unaffected.
    """
    sending = cfg.outreach.sending
    tz, tz_warning = resolve_timezone(sending.timezone)

    now = datetime.now(tz) if tz else datetime.now()
    label = now.strftime("%a %H:%M")
    suffix = "  [" + tz_warning + "]" if tz_warning else ""

    if not sending.enforce_window:
        return True, "sending window not enforced (now {0}){1}".format(label, suffix)

    if sending.weekdays_only and now.weekday() > 4:
        return False, (
            "It is {0}. Cold email is only sent Monday to Friday. "
            "Set outreach.sending.weekdays_only: false to send at weekends, or "
            "enforce_window: false to bypass the whole check.{1}".format(label, suffix)
        )

    minutes = now.hour * 60 + now.minute
    start = sending.start_hour * 60 + sending.start_minute
    end = sending.end_hour * 60 + sending.end_minute

    if start <= minutes < end:
        return True, "{0} {1} is inside the sending window{2}".format(
            label, sending.timezone, suffix
        )

    return False, (
        "It is {0} {1}. Cold email is only sent between {2:02d}:{3:02d} and "
        "{4:02d}:{5:02d}. Set outreach.sending.enforce_window: false to "
        "override.{6}".format(
            label,
            sending.timezone,
            sending.start_hour,
            sending.start_minute,
            sending.end_hour,
            sending.end_minute,
            suffix,
        )
    )


def daily_email_cap(cfg: Config) -> int:
    """The daily sending cap, clamped to the documented hard maximum of 40."""
    configured = int(cfg.outreach.sending.daily_cap)
    hard_max = int(cfg.outreach.sending.hard_max)
    if configured > hard_max:
        log.warning(
            "outreach.sending.daily_cap is %d, above the hard maximum of %d; using %d",
            configured,
            hard_max,
            hard_max,
        )
        return hard_max
    return max(0, configured)


def email_budget_left(cfg: Config, db: Database) -> tuple[int, str]:
    """How many more emails may be sent today."""
    cap = daily_email_cap(cfg)
    used = db.count_sent_today()
    left = max(0, cap - used)
    return left, "{0}/{1} sent today".format(used, cap)


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
