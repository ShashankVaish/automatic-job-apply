"""Ashby application forms (jobs.ashbyhq.com).

Ashby is a React app: inputs are mostly unnamed, labels are real <label>
elements, and custom selects are listbox buttons rather than <select>. The
shared engine handles the labelled text inputs; this filler adds the listbox
handling.
"""
from __future__ import annotations

import logging
from typing import Any

from ats.base import ATSFiller
from ats.fields import FillContext, best_option, label_of

log = logging.getLogger(__name__)

URL_MARKS = ("ashbyhq.com", "jobs.ashbyhq.com")


class AshbyFiller(ATSFiller):
    name = "ashby"
    form_selectors = [
        "form[class*='ashby-application-form']",
        "div[class*='ashby-application-form'] form",
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
            "div[class*='ashby-application-form']",
            "div[class*='ashby-job-posting']",
            "a[href*='ashbyhq.com']",
        ):
            try:
                if page.locator(sel).first.count():
                    return True
            except Exception:
                continue
        return False

    def open_form(self) -> bool:
        for sel in (
            "a:has-text('Apply for this Job')",
            "button:has-text('Apply for this Job')",
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

    def after_fill(self, ctx: FillContext, scope: Any) -> None:
        """Answer Ashby's custom listbox dropdowns, which aren't <select>."""
        try:
            buttons = scope.locator(
                "button[aria-haspopup='listbox'], div[role='combobox'], "
                "button[class*='_select']"
            )
            count = buttons.count()
        except Exception:
            return

        for i in range(min(count, 25)):
            button = buttons.nth(i)
            try:
                if not button.is_visible(timeout=700):
                    continue
                current = (button.inner_text(timeout=1000) or "").strip().lower()
                if current and current not in ("select...", "select", "choose", "-"):
                    continue  # already answered
            except Exception:
                continue

            question = label_of(button)
            if not question:
                continue

            try:
                button.click(timeout=3000)
                self.page.wait_for_timeout(500)
            except Exception:
                continue

            options_loc = self.page.locator("[role='option'], li[role='option']")
            try:
                options = [
                    (t or "").strip()
                    for t in options_loc.all_text_contents()
                    if (t or "").strip()
                ]
            except Exception:
                options = []
            if not options:
                try:
                    button.press("Escape")
                except Exception:
                    pass
                continue

            try:
                answer = ctx.gemini.answer_question(
                    question=question,
                    field_kind="select",
                    options=options,
                    job_title=ctx.job.title,
                    company=ctx.job.company,
                    description=ctx.job.description,
                    resume_link=ctx.resume.public_link,
                )
            except Exception as exc:
                log.warning("Ashby listbox question failed: %s", exc)
                ctx.report.flag("unanswered listbox: " + question[:70])
                continue

            if not answer.confident:
                ctx.report.flag("low-confidence listbox answer: " + question[:70])

            choice = best_option(answer.value, options)
            if choice is None:
                ctx.report.flag("no matching listbox option for: " + question[:70])
                try:
                    button.press("Escape")
                except Exception:
                    pass
                continue

            try:
                options_loc.filter(has_text=choice).first.click(timeout=3000)
                ctx.report.question(question, choice)
                ctx.pace()
            except Exception as exc:
                log.debug("could not pick Ashby option %r: %s", choice, exc)
                ctx.report.flag("could not set listbox: " + question[:70])
