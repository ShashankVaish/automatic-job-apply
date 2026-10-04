"""Workday application forms (*.myworkdayjobs.com, *.myworkdaysite.com).

Workday is the hardest of the lot: it requires an account per employer, spreads
the application over five or more steps, and uses `data-automation-id` instead
of labels. We fill the first page where we can and then stop: anything this
deep and this stateful should not be driven unattended.

So this filler deliberately flags needs_review rather than pretending to
finish. That is the honest outcome, and it matches the spec's instruction to
screenshot unknown multi-step forms and move on.
"""
from __future__ import annotations

import logging
from typing import Any

from ats.base import ATSFiller
from ats.fields import FillContext, set_text

log = logging.getLogger(__name__)

URL_MARKS = (
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "wd1.myworkdayjobs.com",
    "wd3.myworkdayjobs.com",
    "wd5.myworkdayjobs.com",
    "workday.com",
)

# Workday identifies everything by data-automation-id.
KNOWN_FIELDS = [
    ("input[data-automation-id='legalNameSection_firstName']", "first_name"),
    ("input[data-automation-id='legalNameSection_lastName']", "last_name"),
    ("input[data-automation-id='email']", "email"),
    ("input[data-automation-id='phone-number']", "phone"),
    ("input[data-automation-id='addressSection_city']", "location"),
]


class WorkdayFiller(ATSFiller):
    name = "workday"
    form_selectors = [
        "div[data-automation-id='applyFlowPage']",
        "form[data-automation-id='applicationForm']",
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
            "[data-automation-id='applyFlowPage']",
            "[data-automation-id='jobPostingHeader']",
            "[data-automation-id='applyManually']",
        ):
            try:
                if page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    def open_form(self) -> bool:
        """Workday wants an account before it shows a form, so this usually
        fails - which is the right answer."""
        for sel in (
            "a[data-automation-id='applyManually']",
            "a[data-automation-id='adventureButton']",
            "button:has-text('Apply Manually')",
            "a:has-text('Apply')",
        ):
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1000):
                    btn.click(timeout=5000)
                    self.page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        if self.needs_account():
            log.info("Workday is asking for an account; not proceeding")
            return False

        for sel in self.form_selectors:
            try:
                if self.page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    def needs_account(self) -> bool:
        """Workday's sign-in wall. We never create accounts or type passwords."""
        for sel in (
            "[data-automation-id='createAccountLink']",
            "[data-automation-id='signInLink']",
            "input[data-automation-id='password']",
            "button[data-automation-id='createAccountSubmitButton']",
        ):
            try:
                if self.page.locator(sel).first.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        return False

    def before_fill(self, ctx: FillContext, scope: Any) -> None:
        p = ctx.cfg.profile
        values = {
            "first_name": p.first_name(),
            "last_name": p.last_name(),
            "email": p.email,
            "phone": p.phone,
            "location": p.location,
        }
        for selector, kind in KNOWN_FIELDS:
            value = values.get(kind, "")
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
                log.debug("Workday field %s skipped: %s", selector, exc)

    def after_fill(self, ctx: FillContext, scope: Any) -> None:
        if self.needs_account():
            ctx.report.flag(
                "Workday wants an account for this employer - create it yourself, "
                "then re-run"
            )
            return
        ctx.report.flag(
            "Workday applications run over several steps; finish this one by hand"
        )

    def submit_locator(self) -> Any | None:
        """Workday's "Next"/"Save and Continue" is not a submit button, and we
        never want it treated as one."""
        if self.needs_account():
            return None
        for sel in (
            "button[data-automation-id='bottom-navigation-next-button']:has-text('Submit')",
            "button[data-automation-id='submitButton']",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=900):
                    return loc
            except Exception:
                continue
        return None

    def has_next_button(self) -> bool:
        for sel in (
            "button[data-automation-id='bottom-navigation-next-button']",
            "button:has-text('Save and Continue')",
            "button:has-text('Next')",
        ):
            try:
                if self.page.locator(sel).first.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        return False
