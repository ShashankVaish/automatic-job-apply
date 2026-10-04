"""The per-job workflow and the four submit modes.

For each job: scrape -> dedupe -> score with Gemini -> route -> fill -> submit
according to the mode -> log. Every job is wrapped so one failure can't end the
run, and a CAPTCHA on one site never stops the others.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from ai.gemini_client import GeminiClient
from ats import detect as ats_detect
from ats.base import ATSFiller
from ats.fields import FillContext
from browser.human import Pacer, beep, highlight, screenshot
from browser.login import ensure_logged_in
from config import Config
from db import Database
from interact import AssistResult, KeyWatcher, ask_yes_no, wait_for_user_submit
from limits import RunBudget
from models import ApplyOutcome, ApplyTarget, FillReport, Job, SiteBlocked

log = logging.getLogger(__name__)

# Boards we must never auto-submit on, per the spec.
NEVER_AUTO = {"linkedin", "naukri", "indeed"}

SITE_CLASSES: dict[str, tuple[str, str]] = {
    "internshala": ("sites.internshala", "Internshala"),
    "linkedin": ("sites.linkedin", "LinkedIn"),
    "naukri": ("sites.naukri", "Naukri"),
    "indeed": ("sites.indeed", "Indeed"),
}


@dataclass
class SiteStats:
    found: int = 0
    skipped: int = 0
    applied: int = 0
    needs_review: int = 0
    drafted: int = 0
    failed: int = 0

    def bump(self, status: str) -> None:
        if hasattr(self, status):
            setattr(self, status, getattr(self, status) + 1)


@dataclass
class RunStats:
    per_site: dict[str, SiteStats] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def site(self, name: str) -> SiteStats:
        return self.per_site.setdefault(name, SiteStats())

    def totals(self) -> SiteStats:
        total = SiteStats()
        for s in self.per_site.values():
            for key in ("found", "skipped", "applied", "needs_review", "drafted", "failed"):
                setattr(total, key, getattr(total, key) + getattr(s, key))
        return total


def load_source(name: str) -> Any:
    module_name, class_name = SITE_CLASSES[name]
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)


class Runner:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        gemini: GeminiClient,
        session: Any,
        *,
        mode: str = "assist",
        budget: RunBudget | None = None,
        skip_login: bool = False,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.gemini = gemini
        self.session = session
        self.mode = mode
        self.budget = budget or RunBudget(cfg, db)
        self.skip_login = skip_login
        self.stats = RunStats()
        self.shots = cfg.abs_path(cfg.paths.screenshots_dir)
        self.pacer = Pacer(
            cfg.limits.delays.action_min_s,
            cfg.limits.delays.action_max_s,
            cfg.limits.delays.between_apps_min_s,
            cfg.limits.delays.between_apps_max_s,
            enabled=(mode != "dry-run"),
        )
        self.keys: KeyWatcher | None = None

    # ------------------------------------------------------------- top level

    def run(self, sites: list[str]) -> RunStats:
        with KeyWatcher() as keys:
            self.keys = keys
            for site in sites:
                try:
                    self.run_site(site)
                except SiteBlocked as blocked:
                    self.db.block_site(blocked.site, blocked.reason)
                    msg = "{0} stopped for today: {1}".format(blocked.site, blocked.reason)
                    log.error(msg)
                    self.stats.notes.append(msg)
                except Exception as exc:
                    log.exception("%s failed entirely: %s", site, exc)
                    self.stats.notes.append(
                        "{0} failed: {1}: {2}".format(site, type(exc).__name__, exc)
                    )
                if self.budget.run_cap_reached():
                    self.stats.notes.append(
                        "run cap reached ({0})".format(self.budget.remaining_this_run())
                    )
                    break
        self.keys = None
        return self.stats

    def run_site(self, site: str) -> None:
        page = self.session.new_page()

        if not self.skip_login:
            if not ensure_logged_in(page, site):
                self.stats.notes.append(site + ": not signed in, skipped")
                return

        source_cls = load_source(site)
        source = source_cls(
            self.cfg, page, gemini=self.gemini, db=self.db, pacer=self.pacer
        )

        stats = self.stats.site(site)
        log.info("=== %s: starting search ===", site)

        for job in source.search():
            if self.budget.run_cap_reached():
                log.info("Run cap reached; stopping %s", site)
                return

            capped, detail = self.budget.site_cap_reached(site)
            if capped:
                log.info(detail)
                self.stats.notes.append(detail)
                return

            stats.found += 1
            try:
                self.handle_job(source, job)
            except SiteBlocked:
                raise
            except Exception as exc:
                log.exception("Job failed: %s", job.label())
                shot = screenshot(page, self.shots, job.label() + "-error")
                self.record(job, "failed", error="{0}: {1}".format(type(exc).__name__, exc),
                            screenshot=shot)
                stats.failed += 1

    # --------------------------------------------------------- the main flow

    def handle_job(self, source: Any, job: Job) -> None:
        stats = self.stats.site(job.site)
        log.info("--- %s", job.label())

        # 1. dedupe on the cheap fields before spending a page load.
        dup = self.duplicate_reason(job)
        if dup:
            log.info("Skipping (%s)", dup)
            self.record(job, "skipped", error=dup)
            stats.skipped += 1
            return

        # 2. full details and routing.
        job = source.get_job_details(job)
        if not job.description:
            reason = "no job description could be read"
            log.info("Skipping (%s)", reason)
            self.record(job, "skipped", error=reason)
            stats.skipped += 1
            return

        # Dedupe again - the title/company are only reliable after details.
        dup = self.duplicate_reason(job)
        if dup:
            log.info("Skipping (%s)", dup)
            self.record(job, "skipped", error=dup)
            stats.skipped += 1
            return

        # 3. score with Gemini.
        try:
            match = self.gemini.score_match(
                title=job.title,
                company=job.company,
                location=job.location,
                description=job.description,
            )
        except Exception as exc:
            reason = "match scoring failed: {0}: {1}".format(type(exc).__name__, exc)
            log.warning(reason)
            self.record(job, "needs_review", error=reason)
            stats.needs_review += 1
            return

        threshold = self.cfg.matching.match_threshold
        flags = "; ".join(match.red_flags[:3])
        log.info(
            "Score %d (threshold %d), resume=%s. %s%s",
            match.match_score,
            threshold,
            match.best_resume,
            match.reason,
            " [red flags: " + flags + "]" if flags else "",
        )

        if match.match_score < threshold:
            reason = "score {0} < threshold {1}: {2}{3}".format(
                match.match_score,
                threshold,
                match.reason,
                " | red flags: " + flags if flags else "",
            )
            self.record(
                job, "skipped", error=reason, match_score=match.match_score,
                resume_variant=match.best_resume,
            )
            stats.skipped += 1
            return

        resume = self.cfg.resume(match.best_resume)
        if not resume.exists():
            reason = "resume PDF missing for variant '{0}': {1}".format(
                resume.name, resume.abs_pdf_path
            )
            log.error(reason)
            self.record(job, "failed", error=reason, match_score=match.match_score,
                        resume_variant=resume.name)
            stats.failed += 1
            return

        # 4. route and apply.
        self.budget.spend()
        outcome = self.dispatch(source, job, match, resume)

        status = outcome.status
        if status == "applied" and self.mode == "dry-run":
            status = "needs_review"  # nothing is ever "applied" in a dry run

        self.record(
            job,
            status,
            error=outcome.reason or None,
            screenshot=outcome.screenshot or None,
            match_score=match.match_score,
            resume_variant=resume.name,
        )
        stats.bump(status)

        if status in ("applied", "drafted", "needs_review"):
            self.pacer.between_applications()

    def dispatch(self, source: Any, job: Job, match: Any, resume: Any) -> ApplyOutcome:
        """Send the job to the right apply route."""
        if job.apply_target == ApplyTarget.EMAIL:
            return self.apply_by_email(job, resume)

        if job.apply_target == ApplyTarget.CAREER_PAGE:
            capped, detail = self.budget.career_page_cap_reached()
            if capped:
                return ApplyOutcome.skipped(detail)
            return self.apply_career_page(job, match, resume)

        if job.apply_target == ApplyTarget.BOARD:
            return self.apply_in_board(source, job, match, resume)

        return ApplyOutcome.review("no apply route could be determined")

    # -------------------------------------------------------- career pages

    def apply_career_page(self, job: Job, match: Any, resume: Any) -> ApplyOutcome:
        page = self.session.new_page()
        url = job.apply_url()
        log.info("Career page: %s", url)
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            return ApplyOutcome.failed("could not open the career page: " + str(exc))

        filler_cls = ats_detect.detect(url, page)
        filler: ATSFiller = filler_cls(page)
        job.ats = filler.name
        log.info("Hiring system: %s", filler.name)

        if not filler.open_form():
            shot = screenshot(page, self.shots, job.label() + "-no-form")
            return ApplyOutcome.review(
                "no application form found on the career page", screenshot=shot
            )

        report = FillReport(ats=filler.name)
        ctx = FillContext(
            cfg=self.cfg,
            job=job,
            resume=resume,
            gemini=self.gemini,
            report=report,
            pacer=self.pacer,
        )
        filler.fill(ctx)

        if filler.has_next_button() and filler.submit_locator() is None:
            shot = screenshot(page, self.shots, job.label() + "-multistep")
            report.flag("multi-step form - needs a human")
            return ApplyOutcome.review(
                "multi-step form: " + "; ".join(report.review_reasons[:2]),
                screenshot=shot,
                report=report,
            )

        return self.finish(page, filler, job, match, report, allow_auto=True)

    # ------------------------------------------------------ in-board apply

    def apply_in_board(self, source: Any, job: Job, match: Any, resume: Any) -> ApplyOutcome:
        log.info("Using %s's own apply flow", job.site)
        report = FillReport(ats=job.site)
        ctx = FillContext(
            cfg=self.cfg,
            job=job,
            resume=resume,
            gemini=self.gemini,
            report=report,
            pacer=self.pacer,
        )
        outcome = source.apply(job, ctx)
        if outcome.status != "filled":
            if outcome.screenshot:
                return outcome
            shot = screenshot(source.page, self.shots, job.label() + "-" + outcome.status)
            return ApplyOutcome(
                outcome.status, reason=outcome.reason, screenshot=shot, report=outcome.report
            )

        filler = ats_detect.GenericFiller(source.page)
        return self.finish(
            source.page, filler, job, match, report, allow_auto=False
        )

    # ---------------------------------------------------------- email route

    def apply_by_email(self, job: Job, resume: Any) -> ApplyOutcome:
        from email_drafts import draft_application_email

        try:
            path = draft_application_email(self.cfg, self.gemini, job, resume)
        except Exception as exc:
            return ApplyOutcome.failed(
                "could not draft the application email: {0}: {1}".format(
                    type(exc).__name__, exc
                )
            )
        log.info("Drafted an email application at %s", path)
        return ApplyOutcome.drafted("draft saved to " + str(path))

    # -------------------------------------------------------- submit modes

    def finish(
        self,
        page: Any,
        filler: ATSFiller,
        job: Job,
        match: Any,
        report: FillReport,
        *,
        allow_auto: bool,
    ) -> ApplyOutcome:
        """Everything is filled. Decide what to do about Submit."""
        shot = screenshot(page, self.shots, job.label() + "-filled")
        report.screenshot = shot

        if self.mode == "dry-run":
            self.print_summary(job, match, report, mode_note="DRY RUN - nothing submitted")
            return ApplyOutcome("applied", reason="dry run: form filled, not submitted",
                                screenshot=shot, report=report)

        submit = filler.submit_locator()

        if self.mode == "auto":
            return self.finish_auto(page, filler, job, match, report, submit, allow_auto, shot)
        if self.mode == "review":
            return self.finish_review(page, filler, job, match, report, submit, shot)
        return self.finish_assist(page, filler, job, match, report, submit, shot)

    # --- auto -------------------------------------------------------------

    def finish_auto(
        self, page, filler, job, match, report, submit, allow_auto, shot
    ) -> ApplyOutcome:
        threshold = self.cfg.matching.auto_threshold

        if job.site in NEVER_AUTO or not allow_auto:
            report.flag("auto mode is not allowed on " + job.site)
            self.print_summary(job, match, report, mode_note="auto not allowed here")
            return ApplyOutcome.review(
                "auto mode never submits on " + job.site, screenshot=shot, report=report
            )
        if job.apply_target != ApplyTarget.CAREER_PAGE:
            return ApplyOutcome.review(
                "auto mode only submits on company career pages", screenshot=shot, report=report
            )
        if match.match_score < threshold:
            return ApplyOutcome.review(
                "auto mode needs score >= {0} (got {1})".format(threshold, match.match_score),
                screenshot=shot,
                report=report,
            )
        if report.needs_review:
            return ApplyOutcome.review(
                "auto mode refuses a form that needs review: "
                + "; ".join(report.review_reasons[:2]),
                screenshot=shot,
                report=report,
            )
        if submit is None:
            return ApplyOutcome.review(
                "no submit button found", screenshot=shot, report=report
            )

        self.print_summary(job, match, report, mode_note="AUTO - submitting now")
        try:
            submit.click(timeout=15000)
        except Exception as exc:
            return ApplyOutcome.failed("submit click failed: " + str(exc), screenshot=shot)
        return self.confirm(page, filler, job, shot, report)

    # --- review -----------------------------------------------------------

    def finish_review(self, page, filler, job, match, report, submit, shot) -> ApplyOutcome:
        self.print_summary(job, match, report, mode_note="REVIEW - your call")
        if submit is None:
            print("  No submit button found. Finish this one by hand if you want it.")
            return ApplyOutcome.review("no submit button found", screenshot=shot, report=report)

        highlight(submit)
        beep()
        if self.keys is not None:
            self.keys.drain()
        if not ask_yes_no("  Submit this application?", default=False):
            return ApplyOutcome.skipped("you declined in review mode", screenshot=shot)
        try:
            submit.click(timeout=15000)
        except Exception as exc:
            return ApplyOutcome.failed("submit click failed: " + str(exc), screenshot=shot)
        return self.confirm(page, filler, job, shot, report)

    # --- assist (default) -------------------------------------------------

    def finish_assist(self, page, filler, job, match, report, submit, shot) -> ApplyOutcome:
        self.print_summary(job, match, report, mode_note="ASSIST - you click Submit")

        if submit is None:
            print("  Could not find the Submit button - scroll down and look for it.")
        else:
            if highlight(submit):
                print("  The Submit button is highlighted in the browser.")
            else:
                print("  Submit button found but could not be highlighted.")
        beep()
        print("  Click Submit in the browser when you're happy, or press s to skip.")

        if self.keys is not None:
            self.keys.drain()

        result = wait_for_user_submit(
            is_confirmed=filler.confirmed,
            timeout_s=600.0,
            keys=self.keys,
        )

        if result == AssistResult.SUBMITTED:
            final = screenshot(page, self.shots, job.label() + "-confirmed")
            print("  Confirmation page detected - logged as applied.\n")
            return ApplyOutcome.applied(screenshot=final, report=report)
        if result == AssistResult.SKIPPED:
            print("  Skipped.\n")
            return ApplyOutcome.skipped("you pressed s", screenshot=shot)
        if result == AssistResult.QUIT:
            print("  Quitting this job.\n")
            return ApplyOutcome.skipped("you pressed q", screenshot=shot)

        print("  Timed out waiting for a confirmation page - flagged for review.\n")
        return ApplyOutcome.review(
            "no confirmation detected within 10 minutes", screenshot=shot, report=report
        )

    # ----------------------------------------------------------- confirming

    def confirm(self, page, filler, job, shot, report) -> ApplyOutcome:
        """After we clicked Submit ourselves, look for the confirmation page."""
        for _ in range(12):
            page.wait_for_timeout(1500)
            if filler.confirmed():
                final = screenshot(page, self.shots, job.label() + "-confirmed")
                log.info("Confirmed: %s", job.label())
                return ApplyOutcome.applied(screenshot=final, report=report)
        final = screenshot(page, self.shots, job.label() + "-after-submit")
        return ApplyOutcome.review(
            "submitted but no confirmation page was detected", screenshot=final, report=report
        )

    # -------------------------------------------------------------- helpers

    def duplicate_reason(self, job: Job) -> str:
        seen = self.db.seen_url(job.url)
        if seen:
            return "already handled this URL (status: {0})".format(seen)
        if job.company and job.title:
            days = self.cfg.matching.dedupe_days
            dup = self.db.duplicate_company_role(job.company, job.title, days)
            if dup is not None:
                return "duplicate: {0} - {1} already {2} on {3} (within {4} days)".format(
                    dup["company"], dup["role"], dup["status"], dup["date"], days
                )
        return ""

    def record(
        self,
        job: Job,
        status: str,
        *,
        error: str | None = None,
        screenshot: str | None = None,
        match_score: int | None = None,
        resume_variant: str | None = None,
    ) -> None:
        target = job.apply_target.value if isinstance(job.apply_target, ApplyTarget) else None
        self.db.record(
            site=job.site,
            company=job.company or "unknown",
            role=job.title or "unknown",
            url=job.url,
            status=status,
            apply_target=target,
            ats=job.ats or None,
            match_score=match_score,
            resume_variant=resume_variant,
            error=(error or "")[:800] or None,
            screenshot=screenshot,
        )

    def print_summary(
        self, job: Job, match: Any, report: FillReport, *, mode_note: str = ""
    ) -> None:
        width = 72
        print()
        print("=" * width)
        print("  " + job.title)
        print("  " + job.company + ("  |  " + job.location if job.location else ""))
        print("  " + job.apply_url())
        print("-" * width)
        resume_name = self.cfg.resume(getattr(match, "best_resume", "")).name if match else "?"
        print(
            "  score {0}   resume: {1}   via: {2} ({3})".format(
                getattr(match, "match_score", "?"),
                resume_name,
                job.apply_target.value if isinstance(job.apply_target, ApplyTarget) else "?",
                report.ats or "?",
            )
        )
        if match is not None and getattr(match, "reason", ""):
            print("  why: " + match.reason[:140])
        if match is not None and getattr(match, "red_flags", None):
            print("  red flags: " + "; ".join(match.red_flags[:3])[:140])
        print("-" * width)
        print("  Filled:")
        lines = report.summary_lines()
        if lines:
            for line in lines:
                print(line)
        else:
            print("    (nothing)")
        if report.review_reasons:
            print("-" * width)
            print("  NEEDS A LOOK:")
            for reason in report.review_reasons:
                print("    - " + reason)
        if mode_note:
            print("-" * width)
            print("  " + mode_note)
        print("=" * width)
