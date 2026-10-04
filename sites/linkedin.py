"""LinkedIn job search, with Easy Apply as a last resort.

Routing rule from the spec: if a posting's apply button leaves LinkedIn, we
follow it and apply on the company's own form. Easy Apply is only used when
there is no external page, and LinkedIn is never auto-submitted.

LinkedIn changes its markup often, so every selector here has fallbacks and
nothing raises on a miss - a posting we can't read becomes needs_review.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator
from urllib.parse import quote_plus, urlparse

from models import ApplyOutcome, ApplyTarget, Job
from sites.base import JobSource

log = logging.getLogger(__name__)

DATE_FILTERS = {
    "day": "r86400",
    "week": "r604800",
    "month": "r2592000",
    "any": "",
}

CARD_SELECTORS = [
    "div.job-card-container",
    "li.jobs-search-results__list-item",
    "div.job-card-list",
    "li[data-occludable-job-id]",
    "div[data-job-id]",
]

CARD_TITLE_SELECTORS = [
    "a.job-card-container__link",
    "a.job-card-list__title",
    "a.job-card-list__title--link",
    "h3.base-search-card__title",
    "a[data-control-name='job_card_click']",
]

CARD_COMPANY_SELECTORS = [
    "span.job-card-container__primary-description",
    "div.artdeco-entity-lockup__subtitle span",
    "h4.base-search-card__subtitle",
    "a.job-card-container__company-name",
    "div.job-card-container__company-name",
]

CARD_LOCATION_SELECTORS = [
    "ul.job-card-container__metadata-wrapper li",
    "li.job-card-container__metadata-item",
    "span.job-search-card__location",
    "div.artdeco-entity-lockup__caption li",
]

DETAIL_TITLE_SELECTORS = [
    "h1.job-title",
    "h1.t-24",
    "div.job-details-jobs-unified-top-card__job-title h1",
    "h2.top-card-layout__title",
]

DETAIL_COMPANY_SELECTORS = [
    "div.job-details-jobs-unified-top-card__company-name a",
    "div.job-details-jobs-unified-top-card__company-name",
    "a.topcard__org-name-link",
    "span.topcard__flavor",
]

DETAIL_LOCATION_SELECTORS = [
    "div.job-details-jobs-unified-top-card__primary-description-container span.tvm__text",
    "span.topcard__flavor--bullet",
    "div.job-details-jobs-unified-top-card__tertiary-description-container span",
]

DESCRIPTION_SELECTORS = [
    "div.jobs-description__content",
    "div#job-details",
    "div.jobs-box__html-content",
    "div.show-more-less-html__markup",
    "article.jobs-description__container",
]

POSTED_SELECTORS = [
    "span.jobs-unified-top-card__posted-date",
    "div.job-details-jobs-unified-top-card__primary-description-container span:has-text('ago')",
    "span.posted-time-ago__text",
]

APPLY_BUTTON_SELECTORS = [
    "button.jobs-apply-button",
    "div.jobs-apply-button--top-card button",
    "button[aria-label*='Easy Apply']",
    "a.jobs-apply-button",
    "button[aria-label*='Apply']",
]

POPUPS = [
    "button[aria-label='Dismiss']",
    "button.msg-overlay-bubble-header__control",
    "button[aria-label*='Close']",
]


class LinkedIn(JobSource):
    name = "linkedin"
    base_url = "https://www.linkedin.com"
    board_host = "linkedin.com"
    has_own_apply_flow = True

    # ------------------------------------------------------------- searching

    def search_url(self, keyword: str, location: str, start: int = 0) -> str:
        params = [
            "keywords=" + quote_plus(keyword),
            "location=" + quote_plus(location),
        ]
        date_filter = DATE_FILTERS.get((self.source_cfg.date_posted or "week").lower(), "")
        if date_filter:
            params.append("f_TPR=" + date_filter)
        if start:
            params.append("start={0}".format(start))
        return "{0}/jobs/search/?{1}".format(self.base_url, "&".join(params))

    def search(self) -> Iterator[Job]:
        seen: set[str] = set()
        for keyword, location in self.searches():
            for start in (0, 25, 50):
                if len(seen) >= self.max_jobs():
                    return
                url = self.search_url(keyword, location, start)
                log.info("LinkedIn search: %s", url)
                if not self.goto(url):
                    break
                self.dismiss_popups(POPUPS)
                self.pacer.action()
                self._scroll_results()

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

    def _scroll_results(self) -> None:
        """LinkedIn lazy-loads the result list as you scroll it."""
        for _ in range(6):
            try:
                self.page.mouse.wheel(0, 1600)
                self.page.wait_for_timeout(450)
            except Exception:
                break

    def _cards(self) -> list[Job]:
        cards = None
        for sel in CARD_SELECTORS:
            try:
                loc = self.page.locator(sel)
                if loc.count():
                    cards = loc
                    log.debug("LinkedIn cards matched %s (%d)", sel, loc.count())
                    break
            except Exception:
                continue
        if cards is None:
            log.warning("LinkedIn: no job cards found - selectors may have changed")
            return []

        out: list[Job] = []
        for i in range(min(cards.count(), 80)):
            try:
                job = self._card_to_job(cards.nth(i))
                if job.url and job.title:
                    out.append(job)
            except Exception as exc:
                log.debug("LinkedIn card %d unreadable: %s", i, exc)
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
        for sel in CARD_TITLE_SELECTORS + ["a[href*='/jobs/view/']"]:
            try:
                loc = card.locator(sel).first
                if loc.count():
                    href = (loc.get_attribute("href", timeout=2000) or "").strip()
                    if href:
                        break
            except Exception:
                continue

        return Job(
            site=self.name,
            title=pick(CARD_TITLE_SELECTORS),
            company=pick(CARD_COMPANY_SELECTORS),
            location=pick(CARD_LOCATION_SELECTORS),
            url=self.canonical_job_url(self.absolute(href)),
        )

    @staticmethod
    def canonical_job_url(url: str) -> str:
        """Strip LinkedIn's tracking query so dedupe works across searches."""
        if not url:
            return ""
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return "https://www.linkedin.com/jobs/view/{0}/".format(match.group(1))
        try:
            parsed = urlparse(url)
            return "{0}://{1}{2}".format(parsed.scheme, parsed.netloc, parsed.path)
        except Exception:
            return url

    # --------------------------------------------------------------- details

    def get_job_details(self, job: Job) -> Job:
        if not self.goto(job.url):
            return job
        self.dismiss_popups(POPUPS)
        self.pacer.action()
        self._expand_description()

        if not job.title:
            job.title = self.first_text(DETAIL_TITLE_SELECTORS)
        if not job.company:
            job.company = self.first_text(DETAIL_COMPANY_SELECTORS)
        if not job.location:
            job.location = self.first_text(DETAIL_LOCATION_SELECTORS)
        job.posted = job.posted or self.first_text(POSTED_SELECTORS)
        job.description = self.first_text(DESCRIPTION_SELECTORS)

        job.external_url = self._external_apply_url()
        return self.route(job)

    def _expand_description(self) -> None:
        for sel in (
            "button.jobs-description__footer-button",
            "button[aria-label*='see more']",
            "button:has-text('See more')",
            "button.show-more-less-html__button",
        ):
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=800):
                    btn.click(timeout=3000)
                    self.page.wait_for_timeout(400)
                    return
            except Exception:
                continue

    def apply_button(self) -> Any | None:
        for sel in APPLY_BUTTON_SELECTORS:
            try:
                btn = self.page.locator(sel).first
                if btn.count() and btn.is_visible(timeout=1200):
                    return btn
            except Exception:
                continue
        return None

    def is_easy_apply(self) -> bool:
        """Easy Apply keeps you on LinkedIn; anything else leaves."""
        btn = self.apply_button()
        if btn is None:
            return False
        try:
            label = " ".join(
                [
                    (btn.get_attribute("aria-label") or ""),
                    (btn.inner_text(timeout=2000) or ""),
                ]
            ).lower()
        except Exception:
            return False
        return "easy apply" in label

    def _external_apply_url(self) -> str:
        """Find the company page an off-site Apply button would open.

        Tried without clicking first (LinkedIn sometimes exposes the href), and
        only then by opening the popup and reading its URL.
        """
        if self.is_easy_apply():
            return ""

        btn = self.apply_button()
        if btn is None:
            return ""

        try:
            href = btn.get_attribute("href", timeout=1500) or ""
            if href and self.off_board(href):
                return self.absolute(href)
        except Exception:
            pass

        # Clicking opens the employer's site in a new tab. We read the URL and
        # close it; the runner opens it again properly when it applies.
        context = getattr(self.page, "context", None)
        if context is None:
            return ""
        try:
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
                log.info("LinkedIn posting applies externally at %s", url)
                return url
        except Exception as exc:
            log.debug("No external apply popup appeared: %s", exc)
        return ""

    # ------------------------------------------------------ Easy Apply flow

    def apply(self, job: Job, ctx: Any) -> ApplyOutcome:
        """Fill LinkedIn's Easy Apply modal.

        Easy Apply is a multi-step wizard. We fill each step and click Next
        while the steps keep changing, but we never click the final Submit -
        the runner does that only in the modes you allowed.
        """
        from ats.fields import answer_questions, fill_text_fields, upload_resume

        if not self.is_easy_apply():
            return ApplyOutcome.review("not an Easy Apply posting")

        btn = self.apply_button()
        if btn is None:
            return ApplyOutcome.review("the Easy Apply button disappeared")

        try:
            btn.click(timeout=8000)
            self.page.wait_for_timeout(1800)
        except Exception as exc:
            return ApplyOutcome.review("could not open Easy Apply: " + str(exc))

        self.guard()
        ctx.report.ats = "linkedin-easy-apply"

        modal = self._modal()
        if modal is None:
            return ApplyOutcome.review("the Easy Apply modal never appeared")

        for step in range(1, 7):
            log.info("Easy Apply step %d", step)
            upload_resume(modal, ctx)
            leftovers = fill_text_fields(modal, ctx)
            answer_questions(modal, ctx, leftovers)

            if self._has_submit():
                log.info("Reached the Easy Apply review step")
                break
            if not self._click_next():
                ctx.report.flag("Easy Apply had no Next or Submit button on step {0}".format(step))
                break
            self.page.wait_for_timeout(1400)
            modal = self._modal() or modal
        else:
            ctx.report.flag("Easy Apply ran past 6 steps without reaching Submit")

        # Never leave the "follow this company" box ticked by default.
        self._untick_follow(modal)

        if not self._has_submit():
            shot = self._shot(job, "easy-apply-stuck")
            return ApplyOutcome.review(
                "Easy Apply did not reach a submit step: "
                + "; ".join(ctx.report.review_reasons[:2]),
                screenshot=shot,
                report=ctx.report,
            )
        if ctx.report.needs_review:
            return ApplyOutcome.review(
                "; ".join(ctx.report.review_reasons[:3]), report=ctx.report
            )
        return ApplyOutcome("filled", report=ctx.report)

    def _modal(self) -> Any | None:
        for sel in (
            "div.jobs-easy-apply-modal",
            "div[role='dialog'].artdeco-modal",
            "div.jobs-easy-apply-content",
            "div[role='dialog']",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    return loc
            except Exception:
                continue
        return None

    def _has_submit(self) -> bool:
        for sel in (
            "button[aria-label*='Submit application']",
            "button:has-text('Submit application')",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=900):
                    return True
            except Exception:
                continue
        return False

    def _click_next(self) -> bool:
        for sel in (
            "button[aria-label*='Continue to next step']",
            "button[aria-label*='Next']",
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button[aria-label*='Review your application']",
            "button:has-text('Review')",
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=900) and loc.is_enabled(timeout=600):
                    loc.click(timeout=5000)
                    return True
            except Exception:
                continue
        return False

    def _untick_follow(self, modal: Any) -> None:
        for sel in ("input#follow-company-checkbox", "label:has-text('Follow') input"):
            try:
                box = (modal or self.page).locator(sel).first
                if box.count() and box.is_checked(timeout=800):
                    box.uncheck(timeout=2500)
                    log.info("Unticked 'follow this company'")
            except Exception:
                continue

    def _shot(self, job: Job, label: str) -> str:
        from browser.human import screenshot

        return screenshot(
            self.page,
            self.cfg.abs_path(self.cfg.paths.screenshots_dir),
            "{0}-{1}".format(job.label(), label),
        )
