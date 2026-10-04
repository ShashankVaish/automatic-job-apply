"""A stand-in for GeminiClient so the whole pipeline is testable with no API key.

It answers from the profile and some simple rules. It deliberately mirrors the
real client's honesty contract: when it doesn't know, it returns the honest
answer with confident=False so the job gets flagged needs_review.
"""
from __future__ import annotations

import re

from ai.schemas import AnswerResult, EmailDraft, MatchResult
from config import Config


class MockGemini:
    """Drop-in replacement for ai.gemini_client.GeminiClient."""

    def __init__(
        self,
        cfg: Config,
        *,
        score: int = 85,
        resume: str | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.model = "mock"
        self.score = score
        self.resume = resume or (cfg.resumes[0].name if cfg.resumes else "general")
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, str]] = []

    # ---------------------------------------------------------------- utils

    def _maybe_fail(self, kind: str) -> None:
        if kind in self.fail_on:
            raise RuntimeError("mock failure for " + kind)

    def ping(self) -> str:
        return '{"ok": true}'

    # ------------------------------------------------------------ matching

    def score_match(self, *, title, company, location, description) -> MatchResult:
        self._maybe_fail("score_match")
        self.calls.append(("score_match", title))

        text = " ".join([title, description]).lower()
        score = self.score
        flags: list[str] = []

        # Mimic the real prompt's penalties so tests can exercise skipping.
        years = re.search(r"(\d+)\+?\s*(?:to\s*\d+\s*)?years?\s+(?:of\s+)?experience", text)
        if years and int(years.group(1)) > self.cfg.profile.years_of_experience + 1:
            score = min(score, 35)
            flags.append("requires {0}+ years".format(years.group(1)))
        if "unpaid" in text:
            score = min(score, 25)
            flags.append("unpaid")
        if "registration fee" in text or "security deposit" in text:
            score = min(score, 10)
            flags.append("asks for a fee")

        best = self.resume
        for variant in self.cfg.resumes:
            if any(k.lower() in text for k in variant.focus_keywords):
                best = variant.name
                break

        return MatchResult(
            match_score=score,
            best_resume=best,
            reason="mock scoring for tests",
            red_flags=flags,
        )

    # --------------------------------------------------- screening answers

    def answer_question(
        self,
        *,
        question,
        field_kind,
        options,
        job_title,
        company,
        description,
        resume_link="",
    ) -> AnswerResult:
        self._maybe_fail("answer_question")
        self.calls.append(("answer_question", question))
        p = self.cfg.profile
        q = (question or "").lower()

        def pick(*wanted: str) -> str:
            for w in wanted:
                for o in options:
                    if w.lower() == o.strip().lower():
                        return o
                for o in options:
                    if w.lower() in o.strip().lower():
                        return o
            return options[0] if options else ""

        # Numbers first - a years-of-experience box must not get prose.
        if field_kind == "number" or "how many years" in q or "years of experience" in q:
            return AnswerResult(kind="number", value=str(p.years_of_experience))

        if "notice period" in q:
            return AnswerResult(kind=field_kind or "text", value=p.notice_period)
        if "salary" in q or "ctc" in q or "stipend" in q or "compensation" in q:
            return AnswerResult(
                kind=field_kind or "text", value=p.expected_salary or p.expected_stipend
            )

        if field_kind in ("select", "radio", "checkbox") and options:
            if "authoriz" in q or "authoris" in q or "eligible to work" in q:
                return AnswerResult(kind=field_kind, value=pick("yes"))
            if "sponsorship" in q or "visa" in q:
                return AnswerResult(
                    kind=field_kind,
                    value=pick("no" if not p.requires_sponsorship else "yes"),
                )
            if "available" in q or "can you start" in q or "can you join" in q:
                # The profile says notice period "Immediate", so yes is honest.
                immediate = "immediat" in p.notice_period.lower()
                return AnswerResult(
                    kind=field_kind,
                    value=pick("yes" if immediate else "no"),
                    confident=immediate,
                    note="" if immediate else "profile notice period is not immediate",
                )
            if "relocate" in q:
                return AnswerResult(
                    kind=field_kind, value=pick("yes" if p.willing_to_relocate else "no")
                )
            if "remote" in q:
                return AnswerResult(
                    kind=field_kind, value=pick("yes" if p.open_to_remote else "no")
                )
            if "gender" in q or "ethnic" in q or "race" in q or "veteran" in q or "disab" in q:
                return AnswerResult(
                    kind=field_kind,
                    value=pick("decline", "prefer not", "i don't wish", "no"),
                )
            if field_kind == "checkbox":
                known = [
                    o
                    for o in options
                    if any(s.lower() in o.lower() for s in p.skills)
                ]
                if known:
                    return AnswerResult(kind="checkbox", values=known)
            # "Are you comfortable with SQL?" - truthful yes, the profile lists it.
            if any(s.lower() in q for s in p.skills):
                yes = pick("yes")
                if yes:
                    return AnswerResult(kind=field_kind, value=yes)
            # Nothing matched: honest, but say we guessed.
            return AnswerResult(
                kind=field_kind,
                value=pick("no", "none", "not yet") or options[0],
                confident=False,
                note="mock had no rule for this question",
            )

        prose_cues = (
            "cover letter",
            "why do you want",
            "why are you interested",
            "why this",
            "tell us about yourself",
            "describe your",
            "about yourself",
        )
        if any(cue in q for cue in prose_cues):
            return AnswerResult(
                kind="text",
                value=(
                    "I am a {0} targeting {1} roles, with hands-on work in {2}. "
                    "Resume: {3}".format(
                        p.experience_level,
                        (p.target_roles or ["software"])[0],
                        ", ".join(p.skills[:3]),
                        resume_link,
                    )
                ),
            )

        return AnswerResult(
            kind="text",
            value="",
            confident=False,
            note="mock had no rule for this question",
        )

    # ------------------------------------------------------ cover letters

    def cover_letter(self, *, title, company, description, resume_link) -> str:
        self._maybe_fail("cover_letter")
        self.calls.append(("cover_letter", title))
        p = self.cfg.profile
        return (
            "Dear {0} team,\n\n"
            "I am applying for the {1} role. I am a {2} in {3} from {4}, working "
            "mainly with {5}. I would be glad to bring that to your team.\n\n"
            "Thank you for your time.\n{6}\n\nResume: {7}".format(
                company,
                title,
                p.experience_level,
                p.degree,
                p.college,
                ", ".join(p.skills[:3]),
                p.name,
                resume_link,
            )
        )

    # ------------------------------------------------------------- emails

    def email_draft(self, *, title, company, description, resume_link, to_email) -> EmailDraft:
        self._maybe_fail("email_draft")
        self.calls.append(("email_draft", title))
        p = self.cfg.profile
        return EmailDraft(
            subject="Application for {0} - {1}".format(title, p.name),
            body="\n".join(
                [
                    "Hello,",
                    "",
                    "I would like to apply for the {0} role at {1}.".format(title, company),
                    "I am a {0} in {1} from {2}.".format(
                        p.experience_level, p.degree, p.college
                    ),
                    "My main skills are {0}.".format(", ".join(p.skills[:4])),
                    "My resume is attached and linked below.",
                    "",
                    "Resume: " + resume_link,
                    "",
                    "{0}\n{1} | {2}".format(p.name, p.phone, p.email),
                ]
            ),
        )

    # --------------------------------------------------------- follow-ups

    def follow_up(self, *, title, company, applied_on, channel="email") -> str:
        self._maybe_fail("follow_up")
        self.calls.append(("follow_up", title))
        return (
            "I applied for the {0} role at {1} on {2}.\n"
            "I remain very interested in the position.\n"
            "Could you tell me about the next steps?".format(title, company, applied_on)
        )
