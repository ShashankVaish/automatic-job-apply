"""Base class for every job board.

Add a new board by subclassing JobSource and implementing search() and
get_job_details(). Implement apply() only if the board has its own apply flow
worth using (LinkedIn Easy Apply); otherwise leave it and the runner will route
to the company career page.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from ai.gemini_client import GeminiClient
from browser.human import Pacer, screenshot
from browser.login import detect_block
from config import Config, SourceCfg
from db import Database
from models import ApplyOutcome, ApplyTarget, Job, SiteBlocked

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Phrases near an email address that mean "apply by email".
EMAIL_APPLY_HINTS = re.compile(
    r"(send|mail|email|share|forward|drop)\s+(your\s+)?(cv|resume|resumé|application|profile)"
    r"|apply\s+(via|by|through)\s+e?-?mail"
    r"|(cv|resume)s?\s+(to|at)\s*[:\-]?\s*[\w.+-]+@",
    re.I,
)


class JobSource:
    """One job board."""

    name = "base"
    base_url = ""
    # Host fragment used by off_board() to tell "still on the board" from
    # "this links out to the employer".
    board_host = ""
    # Boards whose own apply flow we are allowed to use at all.
    has_own_apply_flow = False

    def __init__(
        self,
        cfg: Config,
        page: Any,
        *,
        gemini: GeminiClient,
        db: Database,
        pacer: Pacer,
    ) -> None:
        self.cfg = cfg
        self.page = page
        self.gemini = gemini
        self.db = db
        self.pacer = pacer
        self.source_cfg: SourceCfg = cfg.source(self.name)

    # ------------------------------------------------------------ interface

    def search(self) -> Iterator[Job]:
        """Yield jobs from the configured searches. Cheap fields only."""
        raise NotImplementedError

    def get_job_details(self, job: Job) -> Job:
        """Open the posting and fill in description, posted date, apply route."""
        raise NotImplementedError

    def apply(self, job: Job, ctx: Any) -> ApplyOutcome:
        """The board's own apply flow. Only used when there's no career page."""
        return ApplyOutcome.review(
            "{0} has no supported in-board apply flow".format(self.name)
        )

    # ------------------------------------------------------------- plumbing

    def goto(self, url: str, *, wait: str = "domcontentloaded") -> bool:
        """Navigate, then check for a CAPTCHA / rate-limit wall."""
        try:
            self.page.goto(url, wait_until=wait)
        except Exception as exc:
            log.warning("%s: navigation to %s failed: %s", self.name, url, exc)
            return False
        self.guard()
        return True

    def guard(self) -> None:
        """Raise SiteBlocked if this page is a challenge or rate-limit wall."""
        reason = detect_block(self.page)
        if reason:
            shot = screenshot(
                self.page,
                self.cfg.abs_path(self.cfg.paths.screenshots_dir),
                "{0}-blocked".format(self.name),
            )
            log.error("%s blocked: %s", self.name, reason)
            raise SiteBlocked(self.name, reason, shot)

    def text_of(self, selector: str, default: str = "") -> str:
        try:
            loc = self.page.locator(selector).first
            if loc.count():
                return (loc.inner_text(timeout=4000) or "").strip()
        except Exception:
            pass
        return default

    def first_text(self, selectors: list[str], default: str = "") -> str:
        for sel in selectors:
            value = self.text_of(sel)
            if value:
                return value
        return default

    def attr_of(self, selector: str, attribute: str, default: str = "") -> str:
        try:
            loc = self.page.locator(selector).first
            if loc.count():
                return (loc.get_attribute(attribute, timeout=3000) or "").strip()
        except Exception:
            pass
        return default

    def absolute(self, href: str) -> str:
        """Resolve a href against the page we're actually on.

        Resolving against a hardcoded base_url instead silently sends relative
        links to the live site - which is wrong on any page we didn't navigate
        to ourselves, and makes local fixtures hit the internet.
        """
        if not href:
            return ""
        if href.startswith("http"):
            return href
        base = ""
        try:
            base = self.page.url or ""
        except Exception:
            base = ""
        if not base or base == "about:blank":
            base = self.base_url or "https://"
        return urljoin(base, href)

    def dismiss_popups(self, selectors: list[str]) -> None:
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=700):
                    loc.click(timeout=2500)
                    self.page.wait_for_timeout(400)
            except Exception:
                continue

    # ----------------------------------------------------------- routing help

    @staticmethod
    def find_apply_email(description: str) -> str:
        """Return an email address only if the text really says to apply there."""
        if not description:
            return ""
        if not EMAIL_APPLY_HINTS.search(description):
            return ""
        matches = EMAIL_RE.findall(description)
        for addr in matches:
            low = addr.lower()
            # Skip addresses that are obviously not an inbox for applications.
            if any(
                bad in low
                for bad in ("noreply", "no-reply", "donotreply", "support@", "info@example")
            ):
                continue
            return addr
        return ""

    @staticmethod
    def is_external(url: str, board_host_fragment: str) -> bool:
        """True when a URL points off the job board."""
        if not url:
            return False
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            return False
        return bool(host) and board_host_fragment not in host

    def off_board(self, href: str) -> bool:
        """True when a href leads somewhere other than this job board.

        The href is resolved against the current page first, so a relative link
        on a live board correctly comes back as on-board. A resolved URL with
        no host at all is a local file (the test fixtures), which is treated as
        off-board so routing can be exercised without touching the internet.
        """
        if not href:
            return False
        resolved = self.absolute(href)
        try:
            host = (urlparse(resolved).netloc or "").lower()
        except Exception:
            return False
        if not host:
            return True
        return self.board_host not in host

    def route(self, job: Job) -> Job:
        """Decide where this job actually gets applied to.

        Order matters: an external career page beats the board's own flow, and
        an explicit "email us your CV" beats everything.
        """
        from ats.detect import detect, is_known_ats_host

        email = self.find_apply_email(job.description)
        if email:
            job.apply_email = email
            job.apply_target = ApplyTarget.EMAIL
            return job

        if job.external_url:
            job.apply_target = ApplyTarget.CAREER_PAGE
            if is_known_ats_host(job.external_url):
                job.ats = detect(job.external_url).name
            return job

        if self.has_own_apply_flow:
            job.apply_target = ApplyTarget.BOARD
            return job

        job.apply_target = ApplyTarget.UNKNOWN
        return job

    # ---------------------------------------------------------------- limits

    def max_jobs(self) -> int:
        return max(1, int(self.source_cfg.max_jobs_per_search or 20))

    def searches(self) -> list[tuple[str, str]]:
        """Every (keyword, location) pair configured for this board."""
        keywords = self.source_cfg.keywords or self.cfg.profile.target_roles
        locations = self.source_cfg.locations or self.cfg.profile.target_locations or [""]
        return [(k, loc) for k in keywords for loc in locations]
