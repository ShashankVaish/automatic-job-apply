"""Best-effort filler for a plain company career-page form.

Used when nothing more specific matched. Relies entirely on the shared engine
in `ats/fields.py`: read every label, fill what we recognise, ask Gemini about
the rest. Anything it can't work out flags the job needs_review.
"""
from __future__ import annotations

import logging
from typing import Any

from ats.base import ATSFiller
from ats.fields import FillContext

log = logging.getLogger(__name__)

# A form worth filling has at least a couple of text inputs or a file input.
APPLY_HINTS = [
    "form input[type='file']",
    "form input[type='email']",
    "form textarea",
    "form input[type='text']",
]

OPEN_FORM_SELECTORS = [
    "a:has-text('Apply for this job')",
    "button:has-text('Apply for this job')",
    "a:has-text('Apply now')",
    "button:has-text('Apply now')",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
    "a[href*='apply']",
]


class GenericFiller(ATSFiller):
    name = "generic"
    form_selectors = [
        "form[action*='apply']",
        "form:has(input[type='file'])",
        "form:has(input[type='email'])",
        "main form",
        "form",
    ]

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return True  # last resort, so it matches anything

    @classmethod
    def matches_page(cls, page: Any) -> bool:
        return True

    def has_form(self) -> bool:
        for sel in APPLY_HINTS:
            try:
                if page_count(self.page, sel) >= 1:
                    return True
            except Exception:
                continue
        return False

    def open_form(self) -> bool:
        """Try to reach a form, clicking an Apply button if one is in the way."""
        if self.has_form():
            return True

        for sel in OPEN_FORM_SELECTORS:
            try:
                btn = self.page.locator(sel).first
                if not btn.count() or not btn.is_visible(timeout=800):
                    continue
                log.info("Generic filler clicking %s", sel)
                btn.click(timeout=6000)
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=12000)
                except Exception:
                    pass
                self.page.wait_for_timeout(1200)
                if self.has_form():
                    return True
            except Exception as exc:
                log.debug("generic open_form %s failed: %s", sel, exc)
                continue
        return self.has_form()

    def after_fill(self, ctx: FillContext, scope: Any) -> None:
        """We never fully trust a form we didn't recognise."""
        if self.has_next_button():
            ctx.report.flag("multi-step form on an unrecognised career page")
        if not ctx.report.fields and not ctx.report.resume_uploaded:
            ctx.report.flag("nothing could be filled on this page")


def page_count(page: Any, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return 0
