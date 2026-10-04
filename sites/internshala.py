"""Internshala: internships and fresher jobs.

Search URLs look like:
  https://internshala.com/internships/keywords-web%20development/
  https://internshala.com/internships/work-from-home-internships/
  https://internshala.com/jobs/keywords-python/

Internshala has its own in-site apply flow, but it is a multi-step modal with
free-text "cover letter" style questions, so we treat it as an in-board flow
and let the runner decide. Postings that link to a company site are routed out
to the career page instead.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator
from urllib.parse import quote

from models import ApplyOutcome, Job
from sites.base import JobSource

log = logging.getLogger(__name__)

CARD_SELECTORS = [
    "div.individual_internship",
    "div.internship_meta",
    "div[internshipid]",
    "div.container-fluid.individual_internship",
]

TITLE_SELECTORS = [
    "h3.job-internship-name",
    "div.profile h3",
    "div.profile a",
    "h3.profile",
    ".heading_4_5 a",
]

COMPANY_SELECTORS = [
    "p.company-name",
    "div.company_name a",
    "div.company_name",
    "a.link_display_like_text",
    ".company h4",
]

LOCATION_SELECTORS = [
    "div#location_names a",
    "p.location_names span a",
    "div.row-1-item.locations span a",
    "#location_names",
]

DESCRIPTION_SELECTORS = [
    "div.internship_details",
    "div.detail_view",
    "div.text-container",
    "div.internship_other_details_container",
]

POPUPS = [
    "button.ns-close",
    "span.close_popup",
    "button:has-text('Maybe later')",
    "div#close_popup",
    "button.btn-secondary:has-text('Not now')",
]


class Internshala(JobSource):
    name = "internshala"
    base_url = "https://internshala.com"
    has_own_apply_flow = True

    # ------------------------------------------------------------- searching

    def search_url(self, keyword: str, location: str) -> str:
        """Internshala uses path segments, not query params."""
        parts: list[str] = []
        loc = (location or "").strip().lower()
        if loc in ("work from home", "work-from-home", "remote", "wfh"):
            parts.append("work-from-home-internships")
        elif loc:
            parts.append("internships-in-" + quote(loc.replace(" ", "-")))
        if keyword.strip():
            parts.append("keywords-" + quote(keyword.strip().replace(" ", "%20"), safe="%"))
        if not parts:
            parts.append("internships")
        return "{0}/internships/{1}/".format(self.base_url, "/".join(parts))

    def search(self) -> Iterator[Job]:
        seen: set[str] = set()
        for keyword, location in self.searches():
            url = self.search_url(keyword, location)
            log.info("Internshala search: %s", url)
            if not self.goto(url):
                continue
            self.dismiss_popups(POPUPS)
            self.pacer.action()

            for job in self._cards():
                if job.url in seen:
                    continue
                seen.add(job.url)
                yield job
                if len(seen) >= self.max_jobs():
                    log.info("Internshala: hit max_jobs_per_search (%d)", self.max_jobs())
                    return

    def _cards(self) -> list[Job]:
        """Read the result cards on the current search page."""
        cards: Any = None
        for sel in CARD_SELECTORS:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    cards = loc
                    log.debug("Internshala cards matched %s (%d)", sel, loc.count())
                    break
            except Exception:
                continue
        if cards is None:
            log.warning("Internshala: no result cards found - selectors may have changed")
            return []

        out: list[Job] = []
        count = min(cards.count(), 80)
        for i in range(count):
            card = cards.nth(i)
            try:
                out.append(self._card_to_job(card))
            except Exception as exc:
                log.debug("Internshala card %d unreadable: %s", i, exc)
        return [j for j in out if j.url and j.title]

    def _card_to_job(self, card: Any) -> Job:
        def pick(selectors: list[str]) -> str:
            for sel in selectors:
                try:
                    loc = card.locator(sel).first
                    if loc.count():
                        txt = (loc.inner_text(timeout=2500) or "").strip()
                        if txt:
                            return " ".join(txt.split())
                except Exception:
                    continue
            return ""

        href = ""
        for sel in ("h3.job-internship-name a", "div.profile a", "a.view_detail_button", "a"):
            try:
                loc = card.locator(sel).first
                if loc.count():
                    href = (loc.get_attribute("href", timeout=2000) or "").strip()
                    if href and "/internship/detail/" in href or "/job/detail/" in href:
                        break
                    if href:
                        break
            except Exception:
                continue

        stipend = pick(["span.stipend", "div.stipend_container span", ".stipend"])

        return Job(
            site=self.name,
            title=pick(TITLE_SELECTORS),
            company=pick(COMPANY_SELECTORS),
            location=pick(LOCATION_SELECTORS),
            url=self.absolute(href),
            stipend=stipend,
            posted=pick(["div.status-success span", "div.posted_by_container", ".status"]),
        )

    # --------------------------------------------------------------- details

    def get_job_details(self, job: Job) -> Job:
        if not self.goto(job.url):
            return job
        self.dismiss_popups(POPUPS)
        self.pacer.action()

        if not job.title:
            job.title = self.first_text(
                ["h1.heading_4_5", "div.profile_on_detail_page", "span.profile_on_detail_page"]
            )
        if not job.company:
            job.company = self.first_text(
                ["div.company_name a", "a.link_display_like_text", "div.company_name"]
            )
        if not job.location:
            job.location = self.first_text(LOCATION_SELECTORS)
        if not job.posted:
            job.posted = self.first_text(
                ["div.status-success span", "div.posted_by_container span", ".status"]
            )
        if not job.stipend:
            job.stipend = self.first_text(["span.stipend", ".stipend"])

        job.description = self._description()
        job.external_url = self._external_link()
        return self.route(job)

    def _description(self) -> str:
        chunks: list[str] = []
        for sel in DESCRIPTION_SELECTORS:
            try:
                loc = self.page.locator(sel).first
                if loc.count():
                    text = (loc.inner_text(timeout=6000) or "").strip()
                    if len(text) > len(" ".join(chunks)):
                        chunks = [text]
            except Exception:
                continue
        if not chunks:
            try:
                chunks = [(self.page.inner_text("body", timeout=8000) or "")[:8000]]
            except Exception:
                chunks = [""]
        text = " ".join(chunks)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # A link with this wording is an external application by definition, so we
    # trust the label rather than comparing hosts.
    EXPLICIT_EXTERNAL = (
        "a:has-text('Apply on company website')",
        "a:has-text('Apply on company site')",
        "a:has-text('company website')",
    )
    # These could be anything, so they only count if they leave internshala.com.
    MAYBE_EXTERNAL = (
        "div.internship_details a[href^='http']",
        "div.text-container a[href^='http']",
    )

    def _external_link(self) -> str:
        """Some Internshala postings point at the company's own form."""
        for sel in self.EXPLICIT_EXTERNAL:
            href = self.attr_of(sel, "href")
            if href:
                resolved = self.absolute(href)
                log.info("Internshala posting links out to %s", resolved)
                return resolved

        for sel in self.MAYBE_EXTERNAL:
            href = self.attr_of(sel, "href")
            if href and self.is_external(href, "internshala.com"):
                log.info("Internshala posting links out to %s", href)
                return href
        return ""

    # ----------------------------------------------------------- in-board apply

    def apply(self, job: Job, ctx: Any) -> ApplyOutcome:
        """Open Internshala's apply modal and fill what we can.

        Internshala's flow is a multi-step modal with employer questions. We
        fill the questions and then hand back to the runner, which highlights
        Submit for you. We never click through its final step automatically.
        """
        from ats.fields import (
            answer_questions,
            ensure_resume_link,
            fill_cover_letter,
            fill_text_fields,
        )

        opened = False
        for sel in (
            "button:has-text('Apply now')",
            "a:has-text('Apply now')",
            "button#continue_button",
            "button.apply_now_cta",
        ):
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1500):
                    btn.click(timeout=6000)
                    self.page.wait_for_timeout(1800)
                    opened = True
                    log.info("Opened Internshala apply modal via %s", sel)
                    break
            except Exception:
                continue

        if not opened:
            return ApplyOutcome.review("could not open the Internshala apply form")

        self.guard()

        modal: Any = self.page
        for sel in ("div#application_form", "div.modal-content form", "form#application_form"):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1200):
                    modal = loc
                    break
            except Exception:
                continue

        leftovers = fill_text_fields(modal, ctx)
        # Internshala's "why should you be hired" box is a cover letter in all
        # but name, so it goes through the same writer.
        fill_cover_letter(modal, ctx)
        answer_questions(modal, ctx, leftovers)
        ensure_resume_link(modal, ctx)
        ctx.report.ats = "internshala"

        if ctx.report.needs_review:
            return ApplyOutcome.review(
                "; ".join(ctx.report.review_reasons[:3]), report=ctx.report
            )
        # The runner handles the submit decision from here.
        return ApplyOutcome("filled", report=ctx.report)
