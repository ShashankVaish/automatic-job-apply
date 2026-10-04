"""Shared data types. Lives at the root so `sites/` and `ats/` can both import
it without a circular dependency."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ApplyTarget(str, Enum):
    """Where the application actually gets submitted."""

    BOARD = "board"           # the job board's own flow (LinkedIn Easy Apply, ...)
    CAREER_PAGE = "career_page"  # a company ATS (Greenhouse, Lever, ...)
    EMAIL = "email"           # the posting says to email a resume
    UNKNOWN = "unknown"


@dataclass
class Job:
    """One scraped job posting."""

    site: str
    title: str
    company: str
    url: str
    location: str = ""
    description: str = ""
    posted: str = ""
    stipend: str = ""

    # Routing, resolved by get_job_details() / the router.
    apply_target: ApplyTarget = ApplyTarget.UNKNOWN
    external_url: str = ""   # company career page, if the posting links out
    apply_email: str = ""    # if the description says to email a resume
    ats: str = ""            # greenhouse | lever | ashby | ... once detected

    extra: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        return "{0} - {1}".format(self.company or "?", self.title or "?")

    def apply_url(self) -> str:
        return self.external_url or self.url


@dataclass
class FillReport:
    """What a filler actually managed to put into a form.

    `needs_review` is set whenever we guessed, hit something we didn't
    understand, or skipped a required field - the job then gets flagged for the
    user to look at instead of being quietly submitted.
    """

    ats: str = ""
    fields: list[tuple[str, str]] = field(default_factory=list)
    resume_uploaded: bool = False
    resume_link_placed: bool = False
    cover_letter: bool = False
    questions: list[tuple[str, str]] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    screenshot: str = ""

    def note(self, name: str, value: str) -> None:
        self.fields.append((name, value))

    def question(self, question: str, answer: str) -> None:
        self.questions.append((question, answer))

    def flag(self, reason: str) -> None:
        self.needs_review = True
        if reason not in self.review_reasons:
            self.review_reasons.append(reason)

    def summary_lines(self) -> list[str]:
        """Human-readable recap, printed in assist/review mode."""
        out: list[str] = []
        for name, value in self.fields:
            shown = value if len(value) <= 70 else value[:67] + "..."
            out.append("  {0:<22} {1}".format(name, shown))
        if self.resume_uploaded:
            out.append("  {0:<22} {1}".format("resume PDF", "uploaded"))
        if self.resume_link_placed:
            out.append("  {0:<22} {1}".format("resume link", "pasted"))
        if self.cover_letter:
            out.append("  {0:<22} {1}".format("cover letter", "written"))
        for q, a in self.questions:
            qs = q if len(q) <= 48 else q[:45] + "..."
            as_ = a if len(a) <= 40 else a[:37] + "..."
            out.append("  Q: {0}\n     -> {1}".format(qs, as_))
        for u in self.unknown_fields:
            out.append("  [not filled] " + u)
        return out


@dataclass
class ApplyOutcome:
    """Result of trying to apply to one job."""

    status: str                     # applied | skipped | failed | needs_review | drafted
    reason: str = ""
    screenshot: str = ""
    report: FillReport | None = None

    @classmethod
    def applied(cls, screenshot: str = "", report: FillReport | None = None) -> "ApplyOutcome":
        return cls("applied", screenshot=screenshot, report=report)

    @classmethod
    def skipped(cls, reason: str, screenshot: str = "") -> "ApplyOutcome":
        return cls("skipped", reason=reason, screenshot=screenshot)

    @classmethod
    def failed(cls, reason: str, screenshot: str = "") -> "ApplyOutcome":
        return cls("failed", reason=reason, screenshot=screenshot)

    @classmethod
    def review(
        cls, reason: str, screenshot: str = "", report: FillReport | None = None
    ) -> "ApplyOutcome":
        return cls("needs_review", reason=reason, screenshot=screenshot, report=report)

    @classmethod
    def drafted(cls, reason: str = "") -> "ApplyOutcome":
        return cls("drafted", reason=reason)


class SiteBlocked(RuntimeError):
    """A CAPTCHA / rate-limit / verification wall. Stops this site for the day."""

    def __init__(self, site: str, reason: str, screenshot: str = "") -> None:
        super().__init__("{0}: {1}".format(site, reason))
        self.site = site
        self.reason = reason
        self.screenshot = screenshot
