"""Base class for every hiring-system filler.

A filler's job is only to fill. It never submits - the runner decides that,
based on the mode you chose.
"""
from __future__ import annotations

import logging
from typing import Any

from ats import fields
from ats.fields import FillContext
from models import FillReport

log = logging.getLogger(__name__)

# Buttons that finish an application, in rough order of confidence.
SUBMIT_SELECTORS = [
    "button#submit_app",
    "input[type='submit'][value*='Submit' i]",
    "button[type='submit']:has-text('Submit')",
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "input[type='submit']",
    "button:has-text('Send application')",
    "button:has-text('Apply now')",
    "button:has-text('Apply')",
    "button[type='submit']",
]

# Things that look like a submit button but move you to the next page instead.
NEXT_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Save and continue')",
    "button[aria-label*='Continue' i]",
]

CONFIRMATION_PHRASES = [
    "thank you for applying",
    "thanks for applying",
    "application received",
    "application submitted",
    "application complete",
    "we have received your application",
    "we've received your application",
    "your application has been submitted",
    "successfully applied",
    "thank you for your interest",
    "thank you for your application",
]


class ATSFiller:
    """Subclass this to support a new hiring system."""

    name = "generic"
    # Narrow the field scan to the real form when we can.
    form_selectors: list[str] = ["form"]

    def __init__(self, page: Any) -> None:
        self.page = page

    # ----------------------------------------------------------- detection

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return False

    @classmethod
    def matches_page(cls, page: Any) -> bool:
        return False

    # -------------------------------------------------------------- filling

    def scope(self) -> Any:
        """The locator we scan for fields. Falls back to the whole page."""
        for sel in self.form_selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    return loc
            except Exception:
                continue
        return self.page

    def open_form(self) -> bool:
        """Click through to the actual application form if it's behind a button.

        Return False if no form could be reached.
        """
        return True

    def fill(self, ctx: FillContext) -> FillReport:
        """Fill the whole form. Override `before_fill`/`after_fill` to tweak."""
        report = ctx.report
        report.ats = self.name

        scope = self.scope()
        self.before_fill(ctx, scope)

        fields.upload_resume(scope, ctx)
        leftovers = fields.fill_text_fields(scope, ctx)
        fields.fill_cover_letter(scope, ctx)
        fields.answer_questions(scope, ctx, leftovers)
        fields.ensure_resume_link(scope, ctx)

        self.after_fill(ctx, scope)
        return report

    def before_fill(self, ctx: FillContext, scope: Any) -> None:
        return None

    def after_fill(self, ctx: FillContext, scope: Any) -> None:
        return None

    # ------------------------------------------------------------ submitting

    def submit_locator(self) -> Any | None:
        """The real submit button, or None if we can't find one."""
        for sel in SUBMIT_SELECTORS:
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1200) and loc.is_enabled(timeout=800):
                    return loc
            except Exception:
                continue
        return None

    def has_next_button(self) -> bool:
        """True when this is a multi-step form we haven't finished."""
        for sel in NEXT_SELECTORS:
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        return False

    def confirmed(self) -> bool:
        """Did the page turn into a 'thanks for applying' page?"""
        try:
            body = (self.page.inner_text("body", timeout=4000) or "").lower()
        except Exception:
            return False
        return any(p in body for p in CONFIRMATION_PHRASES)
