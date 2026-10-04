"""Indeed job search.

Indeed postings either use "Apply now" (Indeed Apply, an in-site flow) or link
straight out to the employer's site. We prefer the external route, and never
auto-submit on Indeed.

Indeed is the most aggressive of the four about bot detection, so the base
class's guard() is called on every navigation and a challenge stops the site
for the rest of the day rather than retrying.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator
from urllib.parse import quote_plus, urlparse

from models import ApplyOutcome, Job
from sites.base import JobSource

log = logging.getLogger(__name__)

CARD_SELECTORS = [
    "div.job_seen_beacon",
    "li div.cardOutline",
    "td.resultContent",
    "div[data-jk]",
]

CARD_TITLE_SELECTORS = [
    "h2.jobTitle span[title]",
    "h2.jobTitle a",
    "a.jcs-JobTitle span",
    "a.jcs-JobTitle",
]
CARD_COMPANY_SELECTORS = [
    "span[data-testid='company-name']",
    "span.companyName",
    "div.company_location span",
]
CARD_LOCATION_SELECTORS = [
    "div[data-testid='text-location']",
    "div.companyLocation",
    "div.company_location div",
]
CARD_SALARY_SELECTORS = [
    "div.salary-snippet-container",
    "div[data-testid='attribute_snippet_testid']",
    "div.metadata.salary-snippet-container",
]

DESCRIPTION_SELECTORS = [
    "div#jobDescriptionText",
    "div.jobsearch-JobComponent-description",
    "div[id='jobDescriptionText']",
]

TITLE_SELECTORS = [
    "h2[data-testid='jobsearch-JobInfoHeader-title']",
    "h1.jobsearch-JobInfoHeader-title",
    "h2.jobsearch-JobInfoHeader-title",
]
COMPANY_SELECTORS = [
    "div[data-testid='inlineHeader-companyName'] a",
    "div[data-company-name='true'] a",
    "div.jobsearch-InlineCompanyRating a",
]
POSTED_SELECTORS = ["span.date", "span[data-testid='myJobsStateDate']"]

POPUPS = [
    "button#onetrust-accept-btn-handler",
    "button[aria-label='close']",
    "button.icl-CloseButton",
    "div.popover-x-button-close",
]


class Indeed(JobSource):
    name = "indeed"
    base_url = "https://in.indeed.com"
    board_host = "indeed.com"
    has_own_apply_flow = True

    # ------------------------------------------------------------- searching

    def search_url(self, keyword: str, location: str, start: int = 0) -> str:
        params = ["q=" + quote_plus(keyword), "l=" + quote_plus(location)]
        days = {"day": "1", "week": "7", "month": "30"}.get(
            (self.source_cfg.date_posted or "week").lower()
        )
        if days:
            params.append("fromage=" + days)
        if start:
            params.append("start={0}".format(start))
        return "{0}/jobs?{1}".format(self.base_url, "&".join(params))

    def search(self) -> Iterator[Job]:
        seen: set[str] = set()
        for keyword, location in self.searches():
            for start in (0, 10, 20):
                if len(seen) >= self.max_jobs():
                    return
                url = self.search_url(keyword, location, start)
                log.info("Indeed search: %s", url)
                if not self.goto(url):
                    break
                self.dismiss_popups(POPUPS)
                self.pacer.action()

                cards = self._cards()
                if not cards:
                    break
                for job in cards:
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
            log.warning("Indeed: no job cards found - selectors may have changed")
            return []

        out: list[Job] = []
        for i in range(min(cards.count(), 60)):
            try:
                job = self._card_to_job(cards.nth(i))
                if job.url and job.title:
                    out.append(job)
            except Exception as exc:
                log.debug("Indeed card %d unreadable: %s", i, exc)
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
        jk = ""
        for sel in ("h2.jobTitle a", "a.jcs-JobTitle", "a[data-jk]"):
            try:
                loc = card.locator(sel).first
                if loc.count():
                    href = (loc.get_attribute("href", timeout=2000) or "").strip()
                    jk = (loc.get_attribute("data-jk", timeout=1500) or "").strip()
                    if href or jk:
                        break
            except Exception:
                continue
        if not jk:
            try:
                jk = (card.get_attribute("data-jk", timeout=1200) or "").strip()
            except Exception:
                jk = ""

        url = (
            "{0}/viewjob?jk={1}".format(self.base_url, jk)
            if jk
            else self.canonical(self.absolute(href))
        )

        job = Job(
            site=self.name,
            title=pick(CARD_TITLE_SELECTORS),
            company=pick(CARD_COMPANY_SELECTORS),
            location=pick(CARD_LOCATION_SELECTORS),
            url=url,
        )
        job.stipend = pick(CARD_SALARY_SELECTORS)
        return job

    @staticmethod
    def canonical(url: str) -> str:
        """Indeed card links carry a long tracking payload; keep jk only."""
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            if "jk=" in (parsed.query or ""):
                for part in parsed.query.split("&"):
                    if part.startswith("jk="):
                        return "https://{0}/viewjob?{1}".format(parsed.netloc, part)
            return "{0}://{1}{2}".format(parsed.scheme, parsed.netloc, parsed.path)
        except Exception:
            return url

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

    def is_indeed_apply(self) -> bool:
        for sel in (
            "button#indeedApplyButton",
            "div.jobsearch-IndeedApplyButton-newDesign",
            "button[aria-label*='Apply now']",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
        return False

    def _external_apply_url(self) -> str:
        if self.is_indeed_apply():
            return ""
        for sel in (
            "a:has-text('Apply on company site')",
            "a#applyButtonLinkContainer a",
            "div#applyButtonLinkContainer a",
            "a[href*='/rc/clk']",
        ):
            try:
                link = self.page.locator(sel).first
                if not link.count() or not link.is_visible(timeout=1000):
                    continue
                href = (link.get_attribute("href", timeout=1500) or "").strip()
                if href and self.off_board(href):
                    resolved = self.absolute(href)
                    log.info("Indeed posting applies externally at %s", resolved)
                    return resolved

                context = getattr(self.page, "context", None)
                if context is None:
                    continue
                with context.expect_page(timeout=12000) as popup_info:
                    link.click(timeout=6000)
                popup = popup_info.value
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                url = popup.url or ""
                popup.close()
                if url and self.off_board(url):
                    return url
            except Exception as exc:
                log.debug("Indeed external apply check failed on %s: %s", sel, exc)
        return ""

    # ------------------------------------------------------ Indeed Apply

    def apply(self, job: Job, ctx: Any) -> ApplyOutcome:
        """Indeed Apply is an iframe wizard. We fill what we can see and hand
        back; Indeed is never auto-submitted."""
        from ats.fields import answer_questions, fill_text_fields, upload_resume

        if not self.is_indeed_apply():
            return ApplyOutcome.review("not an Indeed Apply posting")

        for sel in ("button#indeedApplyButton", "button[aria-label*='Apply now']"):
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1200):
                    btn.click(timeout=8000)
                    self.page.wait_for_timeout(2500)
                    break
            except Exception:
                continue

        self.guard()
        ctx.report.ats = "indeed-apply"

        scope: Any = self.page
        for sel in ("iframe[title*='Apply']", "iframe#indeedapply-modal-iframe"):
            try:
                frame = self.page.frame_locator(sel)
                if frame.locator("form, input").first.count():
                    scope = frame
                    log.info("Indeed Apply form is inside an iframe")
                    break
            except Exception:
                continue

        upload_resume(scope, ctx)
        leftovers = fill_text_fields(scope, ctx)
        answer_questions(scope, ctx, leftovers)

        ctx.report.flag("Indeed Apply is a multi-step wizard - check it before submitting")
        return ApplyOutcome.review(
            "Indeed Apply filled as far as possible; needs your review",
            report=ctx.report,
        )
