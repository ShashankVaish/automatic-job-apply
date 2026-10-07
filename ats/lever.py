"""Lever application forms (jobs.lever.co / jobs.eu.lever.co).

Lever is the simplest of the big ATSes: one page, predictable `name` attributes
(`name`, `email`, `phone`, `urls[LinkedIn]`, `urls[GitHub]`, `comments`), and
custom questions under `cards[...]`.
"""
from __future__ import annotations

import logging
from typing import Any

from ats.base import ATSFiller
from ats.fields import FillContext, set_text

log = logging.getLogger(__name__)

URL_MARKS = ("lever.co", "jobs.lever.co", "jobs.eu.lever.co", "hire.lever.co")


def current_employer(ctx: FillContext) -> str:
    """The candidate's current employer, or "" when there isn't one.

    A fresher has no current company. Putting their college there would read
    as employment history that doesn't exist, so the field is left empty.
    """
    profile = ctx.cfg.profile
    level = (profile.experience_level or "").strip().lower()
    never_employed = profile.years_of_experience <= 0 or level in (
        "fresher",
        "student",
        "graduate",
        "none",
    )
    return "" if never_employed else profile.college


class LeverFiller(ATSFiller):
    name = "lever"
    form_selectors = [
        "form.application-form",
        "form[action*='apply']",
        "div.application-form form",
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
            "form.application-form",
            "div.application-page",
            "input[name='urls[LinkedIn]']",
            "div.postings-btn-wrapper",
        ):
            try:
                if page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    def open_form(self) -> bool:
        """A Lever posting page has an Apply button above the form."""
        for sel in (
            "a.postings-btn:has-text('Apply')",
            "a:has-text('Apply for this job')",
            "a[href*='/apply']",
            "div.postings-btn-wrapper a",
        ):
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1200):
                    btn.click(timeout=5000)
                    self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                    log.info("Clicked Lever apply button (%s)", sel)
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
        """Fill Lever's known-name fields directly - faster and more reliable
        than classifying them, and it stops the generic pass from guessing."""
        p = ctx.cfg.profile
        known = [
            ("input[name='name']", "full_name", p.name),
            ("input[name='email']", "email", p.email),
            ("input[name='phone']", "phone", p.phone),
            # "Current company" is left blank for someone who has never been
            # employed - the college is not a current employer, and claiming it
            # as one is the kind of small untruth this tool must not tell.
            ("input[name='org']", "current_employer", current_employer(ctx)),
            ("input[name='urls[LinkedIn]']", "linkedin", p.linkedin),
            ("input[name='urls[GitHub]']", "github", p.github),
            ("input[name='urls[Portfolio]']", "portfolio", p.portfolio or ctx.resume.public_link),
            ("input[name='urls[Other]']", "resume_link", ctx.resume.public_link),
            ("input[name='location']", "location", p.location),
        ]
        for selector, kind, value in known:
            if not value:
                continue
            try:
                loc = scope.locator(selector).first
                if not loc.count() or not loc.is_visible(timeout=800):
                    continue
                if (loc.input_value(timeout=600) or "").strip():
                    continue
                if set_text(loc, value):
                    ctx.report.note(kind, value)
                    if kind in ("resume_link", "portfolio"):
                        ctx.report.resume_link_placed = True
                    ctx.pace()
            except Exception as exc:
                log.debug("Lever known field %s skipped: %s", selector, exc)

    def after_fill(self, ctx: FillContext, scope: Any) -> None:
        """`comments` is Lever's "additional information" box. If the cover
        letter had nowhere else to go, put the resume link here."""
        if ctx.report.resume_link_placed or not ctx.resume.public_link:
            return
        try:
            box = scope.locator("textarea[name='comments']").first
            if box.count() and box.is_visible(timeout=800):
                if (box.input_value(timeout=600) or "").strip():
                    return
                if set_text(box, "Resume: " + ctx.resume.public_link):
                    ctx.report.note("additional_info", "resume link")
                    ctx.report.resume_link_placed = True
        except Exception as exc:
            log.debug("Lever comments box skipped: %s", exc)
