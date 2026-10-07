"""SmartRecruiters application forms (jobs.smartrecruiters.com, careers.*).

Standard single-page form with predictable field ids (`firstName`, `lastName`,
`email`, `phoneNumber`) plus a consent checkbox that has to be ticked for the
form to submit.
"""
from __future__ import annotations

import logging
from typing import Any

from ats.base import ATSFiller
from ats.fields import FillContext, set_text

log = logging.getLogger(__name__)

URL_MARKS = ("smartrecruiters.com", "jobs.smartrecruiters.com")


class SmartRecruitersFiller(ATSFiller):
    name = "smartrecruiters"
    form_selectors = [
        "form#application-form",
        "form[name='applicationForm']",
        "section.job-application form",
        "main form",
        "form",
    ]

    @classmethod
    def matches_url(cls, url: str) -> bool:
        u = (url or "").lower()
        return any(m in u for m in URL_MARKS)

    @classmethod
    def matches_page(cls, page: Any) -> bool:
        for sel in (
            "input#firstName",
            "form[name='applicationForm']",
            "div.js-smart-apply",
            "a[href*='smartrecruiters.com']",
        ):
            try:
                if page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    def open_form(self) -> bool:
        for sel in (
            "button:has-text('I'm interested')",
            "a:has-text('I'm interested')",
            "button:has-text('Apply')",
            "a:has-text('Apply')",
        ):
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1000):
                    btn.click(timeout=5000)
                    self.page.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        for sel in self.form_selectors:
            try:
                if self.page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    def before_fill(self, ctx: FillContext, scope: Any) -> None:
        p = ctx.cfg.profile
        known = [
            ("input#firstName", "first_name", p.first_name()),
            ("input#lastName", "last_name", p.last_name()),
            ("input#email", "email", p.email),
            ("input#phoneNumber", "phone", p.phone),
            ("input#location", "location", p.location),
            ("input#linkedinProfileUrl", "linkedin", p.linkedin),
            ("input#websiteUrl", "portfolio", p.portfolio or ctx.resume.public_link),
        ]
        for selector, kind, value in known:
            if not value:
                continue
            try:
                loc = scope.locator(selector).first
                if not loc.count() or not loc.is_visible(timeout=700):
                    continue
                if (loc.input_value(timeout=600) or "").strip():
                    continue
                if set_text(loc, value):
                    ctx.report.note(kind, value)
                    ctx.pace()
            except Exception as exc:
                log.debug("SmartRecruiters field %s skipped: %s", selector, exc)

        # Done here rather than in after_fill: the screening-question pass runs
        # in between, and an unticked box would be sent to Gemini as a question.
        self.tick_consent(ctx, scope)

    def tick_consent(self, ctx: FillContext, scope: Any) -> None:
        """Tick the privacy-consent box, which is required to submit.

        This is a factual consent to their privacy policy, not a claim about
        the candidate, so ticking it is safe.
        """
        for sel in (
            "input#consent",
            "input[name='consent']",
            "input[type='checkbox'][required]",
            "label:has-text('privacy') input[type='checkbox']",
        ):
            try:
                box = scope.locator(sel).first
                if not box.count() or not box.is_visible(timeout=700):
                    continue
                if box.is_checked(timeout=600):
                    return
                box.check(timeout=2500)
                ctx.report.note("consent", "privacy policy accepted")
                return
            except Exception as exc:
                log.debug("SmartRecruiters consent box %s skipped: %s", sel, exc)
