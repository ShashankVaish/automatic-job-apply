"""Work out which hiring system a career page is running, and build its filler.

URL first (cheap and usually decisive), then page markup, then the generic
filler as a last resort.
"""
from __future__ import annotations

import logging
from typing import Any, Type

from ats.base import ATSFiller
from ats.generic import GenericFiller

log = logging.getLogger(__name__)


def _registry() -> list[Type[ATSFiller]]:
    """Specific fillers, most-specific first. Imported lazily so a module that
    isn't written yet can't break the run."""
    found: list[Type[ATSFiller]] = []
    for module_name, class_name in (
        ("ats.greenhouse", "GreenhouseFiller"),
        ("ats.lever", "LeverFiller"),
        ("ats.ashby", "AshbyFiller"),
        ("ats.smartrecruiters", "SmartRecruitersFiller"),
        ("ats.workday", "WorkdayFiller"),
    ):
        try:
            module = __import__(module_name, fromlist=[class_name])
            found.append(getattr(module, class_name))
        except (ImportError, AttributeError) as exc:
            log.debug("filler %s unavailable: %s", module_name, exc)
    return found


def detect(url: str, page: Any | None = None) -> Type[ATSFiller]:
    """Return the filler class for this page."""
    fillers = _registry()

    for filler in fillers:
        try:
            if filler.matches_url(url):
                log.info("Detected %s from the URL", filler.name)
                return filler
        except Exception:
            continue

    if page is not None:
        for filler in fillers:
            try:
                if filler.matches_page(page):
                    log.info("Detected %s from the page markup", filler.name)
                    return filler
            except Exception:
                continue

    log.info("No specific ATS matched; using the generic filler")
    return GenericFiller


def build(url: str, page: Any) -> ATSFiller:
    return detect(url, page)(page)


# Hosts that are definitely an application form rather than a marketing page.
KNOWN_ATS_HOSTS = (
    "greenhouse.io",
    "grnh.se",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "wd1.myworkdayjobs.com",
    "wd3.myworkdayjobs.com",
    "wd5.myworkdayjobs.com",
    "smartrecruiters.com",
    "jobvite.com",
    "icims.com",
    "workable.com",
    "recruitee.com",
    "teamtailor.com",
    "breezy.hr",
    "bamboohr.com",
    "zohorecruit.com",
    "keka.com",
    "darwinbox.in",
)


def is_known_ats_host(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in KNOWN_ATS_HOSTS)
