"""Naukri.com job search.

Naukri postings usually either have an in-site "Apply" button or an "Apply on
company site" button that leaves for the employer's own form. We prefer the
latter, per the routing rule, and never auto-submit on Naukri.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator
from urllib.parse import quote_plus

from models import ApplyOutcome, Job
from sites.base import JobSource

log = logging.getLogger(__name__)

CARD_SELECTORS = [
    "div.srp-jobtuple-wrapper",
    "article.jobTuple",
    "div.jobTuple",
    "div[data-job-id]",
]

CARD_TITLE_SELECTORS = ["a.title", "a.jobTupleHeader__title", "h2 a", "a.job-title"]
CARD_COMPANY_SELECTORS = [
    "a.comp-name",
    "a.subTitle",
    "span.comp-name",
    "div.companyInfo a",
]
CARD_LOCATION_SELECTORS = [
    "span.locWdth",
    "span.loc-wrap span",
    "li.location span",
    "span.ellipsis.fleft.locWdth",
]
CARD_EXPERIENCE_SELECTORS = ["span.expwdth", "li.experience span", "span.exp-wrap span"]

DESCRIPTION_SELECTORS = [
    "div.styles_JDC__dang-inner-html__h0K4t",
    "section.job-desc",
    "div.dang-inner-html",
    "div.job-description",
    "div.JDC__dang-inner-html",
]

TITLE_SELECTORS = [
    "h1.styles_jd-header-title__rZwM1",
    "h1.jd-header-title",
    "header h1",
]
COMPANY_SELECTORS = [
    "div.styles_jd-header-comp-name__MvqAI a",
    "a.jd-header-comp-name",
    "div.jd-header-comp-name a",
]
POSTED_SELECTORS = [
    "span.styles_jhc__stat__PgY67:has-text('Posted')",
    "span.jd-stats:has-text('Posted')",
    "label:has-text('Posted')",
]

POPUPS = [
    "span.crossIcon",
    "div.chatbot_Nav span.chatbot_close",
    "button:has-text('Maybe later')",
    "a.close",
]


class Naukri(JobSource):
    name = "naukri"
    base_url = "https://www.naukri.com"
    board_host = "naukri.com"
    has_own_apply_flow = True

    # ------------------------------------------------------------- searching

    def search_url(self, keyword: str, location: str) -> str:
        """Naukri uses SEO paths: /python-developer-jobs-in-noida"""
        slug = re.sub(r"[^a-z0-9]+", "-", (keyword or "").strip().lower()).strip("-")
        path = (slug or "jobs") + "-jobs"
        if location.strip():
            loc = re.sub(r"[^a-z0-9]+", "-", location.strip().lower()).strip("-")
            path += "-in-" + loc
        experience = self.source_cfg.experience_years
        query = "?k={0}&experience={1}".format(quote_plus(keyword), experience)
        return "{0}/{1}{2}".format(self.base_url, path, query)

    def search(self) -> Iterator[Job]:
        seen: set[str] = set()
        for keyword, location in self.searches():
            url = self.search_url(keyword, location)
            log.info("Naukri search: %s", url)
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
                    return

    def _cards(self) -> list[Job]:
        cards = None
        for sel in CARD_SELECTORS:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    cards = loc
                    break
            except Exception:
                continue
        if cards is None:
            log.warning("Naukri: no job cards found - selectors may have changed")
            return []

        out: list[Job] = []
        for i in range(min(cards.count(), 80)):
            try:
                job = self._card_to_job(cards.nth(i))
                if job.url and job.title:
                    out.append(job)
            except Exception as exc:
                log.debug("Naukri card %d unreadable: %s", i, exc)
        return out

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
        for sel in CARD_TITLE_SELECTORS + ["a[href*='/job-listings-']"]:
            try:
                loc = card.locator(sel).first
                if loc.count():
                    href = (loc.get_attribute("href", timeout=2000) or "").strip()
                    if href:
                        break
            except Exception:
                continue

        job = Job(
            site=self.name,
            title=pick(CARD_TITLE_SELECTORS),
            company=pick(CARD_COMPANY_SELECTORS),
            location=pick(CARD_LOCATION_SELECTORS),
            url=self.absolute(href.split("?")[0]),
        )
        job.extra["experience"] = pick(CARD_EXPERIENCE_SELECTORS)
        return job

    # --------------------------------------------------------------- details

    def get_job_details(self, job: Job) -> Job:
        if not self.goto(job.url):
            return job
        self.dismiss_popups(POPUPS)
        self.pacer.action()

        if not job.title:
            job.title = self.first_text(TITLE_SELECTORS)
        if not job.company:
            job.company = self.first_text(COMPANY_SELECTORS)
        job.posted = job.posted or self.first_text(POSTED_SELECTORS)
        job.description = self.first_text(DESCRIPTION_SELECTORS)
        job.external_url = self._external_apply_url()
        return self.route(job)

    def _external_apply_url(self) -> str:
        """"Apply on company site" leaves Naukri for the employer's form."""
        for sel in (
            "button#company-site-button",
            "a#company-site-button",
            "button:has-text('Apply on company site')",
            "a:has-text('Apply on company site')",
        ):
            try:
                btn = self.page.locator(sel).first
                if not btn.count() or not btn.is_visible(timeout=1000):
                    continue
                href = btn.get_attribute("href", timeout=1500) or ""
                if href and self.off_board(href):
                    return self.absolute(href)

                context = getattr(self.page, "context", None)
                if context is None:
                    continue
                with context.expect_page(timeout=12000) as popup_info:
                    btn.click(timeout=6000)
                popup = popup_info.value
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                url = popup.url or ""
                popup.close()
                if url and self.off_board(url):
                    log.info("Naukri posting applies externally at %s", url)
                    return url
            except Exception as exc:
                log.debug("Naukri external apply check failed on %s: %s", sel, exc)
        return ""

    # ----------------------------------------------------- in-site apply

    def apply(self, job: Job, ctx: Any) -> ApplyOutcome:
        """Naukri's in-site apply is usually one click, sometimes followed by a
        chatbot asking screening questions. We never answer the chatbot blind."""
        from ats.fields import answer_questions, fill_text_fields

        btn = None
        for sel in ("button#apply-button", "button:has-text('Apply')", "a#apply-button"):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1200):
                    btn = loc
                    break
            except Exception:
                continue
        if btn is None:
            return ApplyOutcome.review("no Naukri apply button found")

        ctx.report.ats = "naukri"
        ctx.report.flag(
            "Naukri's in-site apply submits on the first click, so it is left for you"
        )

        # Naukri's own flow has no reviewable form before submitting, so we
        # deliberately stop here and let you press the button.
        chatbot = self.page.locator("div.chatbot_DrawerContentWrapper").first
        try:
            if chatbot.count() and chatbot.is_visible(timeout=800):
                leftovers = fill_text_fields(chatbot, ctx)
                answer_questions(chatbot, ctx, leftovers)
        except Exception as exc:
            log.debug("Naukri chatbot not handled: %s", exc)

        return ApplyOutcome.review(
            "Naukri in-site apply needs your click", report=ctx.report
        )
