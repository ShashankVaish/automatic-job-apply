"""Greenhouse application forms.

Two shapes in the wild:
  - job-boards.greenhouse.io / boards.greenhouse.io hosted boards
  - the same form embedded in a company page via #grnhse_app (an iframe) or
    the newer job-boards.greenhouse.io embed
"""
from __future__ import annotations

import logging
from typing import Any

from ats.base import ATSFiller
from ats.fields import FillContext

log = logging.getLogger(__name__)

URL_MARKS = ("greenhouse.io", "grnh.se", "job-boards.greenhouse.io")


class GreenhouseFiller(ATSFiller):
    name = "greenhouse"
    form_selectors = [
        "form#application-form",
        "form#application_form",
        "div#application div form",
        "main form",
        "form",
    ]

    @classmethod
    def matches_url(cls, url: str) -> bool:
        u = (url or "").lower()
        return any(m in u for m in URL_MARKS)

    @classmethod
    def matches_page(cls, page: Any) -> bool:
        # Only greenhouse-specific markers. `form#application-form` on its own
        # is far too common - SmartRecruiters uses that id too.
        for sel in (
            "#grnhse_app",
            "iframe[src*='greenhouse.io']",
            "input[name^='job_application']",
            "[data-mapped='true'][id^='greenhouse']",
        ):
            try:
                if page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        try:
            html = page.content()[:200000].lower()
        except Exception:
            return False
        return "greenhouse.io" in html

    def frame_scope(self) -> Any:
        """Greenhouse embeds live in an iframe; work inside it when present."""
        for sel in ("iframe#grnhse_iframe", "iframe[src*='greenhouse.io']"):
            try:
                el = self.page.locator(sel).first
                if el.count() and el.is_visible(timeout=1500):
                    frame = el.frame_locator(":scope")
                    log.info("Greenhouse form is inside an iframe")
                    return frame
            except Exception:
                continue
        return None

    def scope(self) -> Any:
        frame = self.frame_scope()
        if frame is not None:
            for sel in self.form_selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.count():
                        return loc
                except Exception:
                    continue
            return frame
        return super().scope()

    def open_form(self) -> bool:
        """Newer Greenhouse boards hide the form behind an "Apply" button."""
        for sel in (
            "a:has-text('Apply for this job')",
            "button:has-text('Apply for this job')",
            "a#apply_button",
            "button:has-text('Apply now')",
        ):
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1200):
                    btn.click(timeout=5000)
                    self.page.wait_for_timeout(1500)
                    log.info("Clicked Greenhouse apply button (%s)", sel)
                    break
            except Exception:
                continue

        for sel in self.form_selectors:
            try:
                if self.page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return self.frame_scope() is not None

    def before_fill(self, ctx: FillContext, scope: Any) -> None:
        # Greenhouse marks required questions with a red asterisk in a span,
        # which our label reader already picks up. Nothing extra needed, but
        # dismiss the cookie banner so it can't cover the submit button.
        for sel in (
            "button:has-text('Accept')",
            "button:has-text('Got it')",
            "#onetrust-accept-btn-handler",
        ):
            try:
                b = self.page.locator(sel).first
                if b.count() and b.is_visible(timeout=700):
                    b.click(timeout=2500)
                    break
            except Exception:
                continue
