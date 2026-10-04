"""Gemini wrapper: match scoring, screening answers, cover letters, emails, follow-ups.

Hard rule enforced in every prompt: never claim experience, skills, degrees or
certifications that are not in the profile. When the honest answer is unclear the
model must say so (confident=false) and the job gets flagged needs_review.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ai.schemas import AnswerResult, CoverLetter, EmailDraft, FollowUp, MatchResult
from config import Config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

HONESTY_RULE = (
    "ABSOLUTE RULES:\n"
    "1. Never invent or exaggerate experience, skills, degrees, certifications, "
    "employers or availability. Use ONLY facts from the candidate profile.\n"
    "2. If the profile does not contain the answer, pick the honest answer "
    "(usually 'No', 0, or the lowest truthful option) and set confident=false.\n"
    "3. Never claim years of experience above what the profile states.\n"
    "4. Reply with JSON only. No markdown fences, no commentary.\n"
)


class GeminiUnavailable(RuntimeError):
    """The SDK is missing or no API key was configured."""


class GeminiInvalidJSON(RuntimeError):
    """Model replied with something that isn't the schema we asked for."""


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    # Some models prepend prose; grab the outermost JSON object.
    if not t.startswith("{"):
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            t = t[start : end + 1]
    return t.strip()


def _truncate(text: str, limit: int = 12000) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "\n...[truncated]"


class GeminiClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.model = cfg.gemini_model
        self._client: Any = None

    # ------------------------------------------------------------------ setup

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.cfg.gemini_api_key:
                raise GeminiUnavailable(
                    "GEMINI_API_KEY is not set; add it to .env"
                )
            try:
                from google import genai  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover
                raise GeminiUnavailable(
                    "google-genai is not installed; run: pip install -r requirements.txt"
                ) from exc
            self._client = genai.Client(api_key=self.cfg.gemini_api_key)
        return self._client

    def ping(self) -> str:
        """One tiny call, used by `main.py --check` to prove the key works."""
        resp = self.client.models.generate_content(
            model=self.model,
            contents='Reply with exactly this JSON: {"ok": true}',
            config={"response_mime_type": "application/json", "temperature": 0},
        )
        return (resp.text or "").strip()

    # ------------------------------------------------------------- transport

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((GeminiInvalidJSON, Exception)),
    )
    def _ask(self, prompt: str, schema: Type[T], *, temperature: float = 0.2) -> T:
        """Call Gemini, parse JSON, validate against `schema`.

        Retries with exponential backoff on both transport errors and invalid
        JSON. Raises on final failure so callers can fall back to needs_review.
        """
        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "temperature": temperature,
            },
        )
        raw = _strip_fences(resp.text or "")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("Gemini returned non-JSON: %s", raw[:300])
            raise GeminiInvalidJSON(str(exc)) from exc
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            log.warning("Gemini JSON failed %s validation: %s", schema.__name__, exc)
            raise GeminiInvalidJSON(str(exc)) from exc

    # ----------------------------------------------------------- 1. matching

    def score_match(
        self, *, title: str, company: str, location: str, description: str
    ) -> MatchResult:
        variants = [
            {
                "name": r.name,
                "focus_keywords": r.focus_keywords,
            }
            for r in self.cfg.resumes
        ]
        prompt = (
            "You are screening a job for a candidate. Decide how well it fits and "
            "which resume variant to send.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE PROFILE:\n"
            + self.cfg.profile.as_prompt_block()
            + "\n\nRESUME VARIANTS AVAILABLE:\n"
            + json.dumps(variants, indent=2)
            + "\n\nJOB:\nTitle: "
            + title
            + "\nCompany: "
            + company
            + "\nLocation: "
            + location
            + "\nDescription:\n"
            + _truncate(description)
            + "\n\nScore 0-100 on fit with the candidate's target roles, locations, "
            "skills and experience level. Penalise heavily (score under 40) if the "
            "job demands more years of experience than the candidate has, requires "
            "skills they lack, is in an unwanted location, or is unpaid/a scam.\n"
            "red_flags should list concrete concerns such as 'requires 3+ years', "
            "'unpaid', 'sales role', 'registration fee'.\n"
            'best_resume must be exactly one of the variant names above.\n'
            'Reply as JSON: {"match_score": <int 0-100>, "best_resume": "<variant name>", '
            '"reason": "<one or two sentences>", "red_flags": ["..."]}'
        )
        result = self._ask(prompt, MatchResult)
        # Guard against a hallucinated variant name.
        known = {r.name.lower() for r in self.cfg.resumes}
        if result.best_resume.lower() not in known:
            log.warning(
                "Gemini picked unknown resume %r; falling back to %s",
                result.best_resume,
                self.cfg.resumes[0].name,
            )
            result.best_resume = self.cfg.resumes[0].name
        return result

    # -------------------------------------------- 2. screening questions

    def answer_question(
        self,
        *,
        question: str,
        field_kind: str,
        options: list[str],
        job_title: str,
        company: str,
        description: str,
        resume_link: str = "",
    ) -> AnswerResult:
        opts = json.dumps(options) if options else "[]"
        prompt = (
            "Answer one job-application screening question as the candidate.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE PROFILE:\n"
            + self.cfg.profile.as_prompt_block()
            + ("\nResume link: " + resume_link if resume_link else "")
            + "\n\nJOB: "
            + job_title
            + " at "
            + company
            + "\nJOB DESCRIPTION (for context only):\n"
            + _truncate(description, 4000)
            + "\n\nQUESTION: "
            + question
            + "\nFIELD TYPE: "
            + field_kind
            + "\nALLOWED OPTIONS: "
            + opts
            + "\n\nRules for the reply:\n"
            "- If ALLOWED OPTIONS is non-empty, `value` (or every item of `values`) "
            "MUST be copied character-for-character from that list.\n"
            "- kind must match FIELD TYPE: text, number, select, radio, checkbox, "
            "or skip when you genuinely cannot answer honestly.\n"
            "- For checkbox questions put every chosen option in `values`.\n"
            "- For numbers reply with digits only, e.g. \"0\" or \"20000\".\n"
            "- Keep free-text answers under 60 words, first person, concrete.\n"
            "- Set confident=false whenever you had to guess, and explain in `note`.\n"
            '\nReply as JSON: {"kind": "...", "value": "...", "values": [], '
            '"confident": true|false, "note": "..."}'
        )
        return self._ask(prompt, AnswerResult)

    # -------------------------------------------------- 3. cover letters

    def cover_letter(
        self,
        *,
        title: str,
        company: str,
        description: str,
        resume_link: str,
    ) -> str:
        prompt = (
            "Write a tailored cover letter for this application.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE PROFILE:\n"
            + self.cfg.profile.as_prompt_block()
            + "\n\nJOB: "
            + title
            + " at "
            + company
            + "\nDESCRIPTION:\n"
            + _truncate(description, 6000)
            + "\n\nRequirements:\n"
            "- Strictly under 150 words.\n"
            "- Plain text, no markdown, no placeholders like [Company].\n"
            "- Open by naming the role and company, then two or three sentences "
            "connecting the candidate's real skills and projects to the role.\n"
            "- Mention nothing the profile does not support.\n"
            "- The LAST line must be exactly: Resume: " + resume_link + "\n"
            '\nReply as JSON: {"text": "<the letter>"}'
        )
        letter = self._ask(prompt, CoverLetter, temperature=0.4).text
        if resume_link and resume_link not in letter:
            letter = letter.rstrip() + "\n\nResume: " + resume_link
        return letter

    # --------------------------------------------- 4. email applications

    def email_draft(
        self,
        *,
        title: str,
        company: str,
        description: str,
        resume_link: str,
        to_email: str,
    ) -> EmailDraft:
        prompt = (
            "Draft a short job-application email.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE PROFILE:\n"
            + self.cfg.profile.as_prompt_block()
            + "\n\nAPPLYING FOR: "
            + title
            + " at "
            + company
            + "\nSENDING TO: "
            + to_email
            + "\nJOB DESCRIPTION:\n"
            + _truncate(description, 5000)
            + "\n\nRequirements:\n"
            "- subject: concise, includes the role name.\n"
            "- body: 5 to 8 short lines, plain text, first person.\n"
            "- Mention that the resume is attached and linked.\n"
            "- Second-to-last line must be: Resume: " + resume_link + "\n"
            "- Sign off with the candidate's name, phone and email.\n"
            '\nReply as JSON: {"subject": "...", "body": "..."}'
        )
        draft = self._ask(prompt, EmailDraft, temperature=0.4)
        if resume_link and resume_link not in draft.body:
            draft.body = draft.body.rstrip() + "\n\nResume: " + resume_link
        return draft

    # -------------------------------------------- 5. cold email outreach

    def outreach_email(
        self,
        *,
        company: str,
        role: str,
        contact_name: str = "",
        company_context: str = "",
        resume_link: str = "",
    ) -> EmailDraft:
        """A cold introduction email. No job posting required.

        This is a stranger's inbox, so the prompt is stricter than the
        application prompt: short, specific, no flattery, no fabricated
        knowledge about the company, and an easy opt-out.
        """
        greeting = "Hi " + contact_name if contact_name else "Hello"
        prompt = (
            "Write a short cold outreach email from a job seeker to someone at a "
            "company that has not advertised a role.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE PROFILE:\n"
            + self.cfg.profile.as_prompt_block()
            + "\n\nCOMPANY: "
            + company
            + "\nROLE THEY ARE INTERESTED IN: "
            + (role or "any suitable entry-level role")
            + "\nCONTACT NAME: "
            + (contact_name or "(unknown - use a neutral greeting)")
            + "\nWHAT WE KNOW ABOUT THE COMPANY (may be empty):\n"
            + _truncate(company_context, 3000)
            + "\n\nHard requirements:\n"
            "- subject: under 60 characters, specific, no clickbait, no ALL CAPS.\n"
            "- body: 90 to 140 words, 5 to 8 short lines, plain text.\n"
            "- Open with '" + greeting + ",'.\n"
            "- Say in one line who the candidate is and what they are looking for.\n"
            "- Give two concrete, truthful specifics from the profile (skills or "
            "degree). Never invent projects, metrics, employers or achievements.\n"
            "- Say nothing about the company that is not in the context above. If "
            "the context is empty, do not pretend to know their product.\n"
            "- No flattery ('huge fan', 'love what you're building'), no hype, no "
            "buzzwords, no emoji, no exclamation marks.\n"
            "- One clear ask: a short conversation or whether they are hiring.\n"
            "- Include a one-line polite opt-out, e.g. 'If this isn't the right "
            "time, no problem at all.'\n"
            "- Second-to-last line must be: Resume: " + resume_link + "\n"
            "- Sign off with the candidate's name, phone and email.\n"
            '\nReply as JSON: {"subject": "...", "body": "..."}'
        )
        draft = self._ask(prompt, EmailDraft, temperature=0.5)
        if resume_link and resume_link not in draft.body:
            draft.body = draft.body.rstrip() + "\n\nResume: " + resume_link
        return draft

    def outreach_followup(
        self,
        *,
        company: str,
        role: str,
        contact_name: str,
        first_contacted: str,
        stage: int,
        resume_link: str = "",
    ) -> EmailDraft:
        """Follow-up 1 or 2 on a cold email that got no reply."""
        tone = (
            "This is the FIRST follow-up. Keep it to 3 or 4 short lines: a one-line "
            "reminder of the original email with its date, one new truthful detail "
            "from the profile, and the same light ask."
            if stage <= 1
            else "This is the SECOND and FINAL follow-up. Keep it to 3 lines, say "
            "explicitly that this is the last time you'll reach out, and leave the "
            "door open without any guilt-tripping."
        )
        prompt = (
            "Write a follow-up to a cold outreach email that received no reply.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE PROFILE:\n"
            + self.cfg.profile.as_prompt_block()
            + "\n\nCOMPANY: "
            + company
            + "\nROLE: "
            + (role or "any suitable entry-level role")
            + "\nCONTACT NAME: "
            + (contact_name or "(unknown)")
            + "\nFIRST CONTACTED: "
            + first_contacted
            + "\nFOLLOW-UP NUMBER: "
            + str(max(1, stage))
            + "\n\n"
            + tone
            + "\n- subject: reply-style, e.g. 'Re: <original topic>'.\n"
            "- No pressure, no guilt, no 'just bumping this to the top of your "
            "inbox', no implication that they were rude not to reply.\n"
            "- Last line must be: Resume: " + resume_link + "\n"
            '\nReply as JSON: {"subject": "...", "body": "..."}'
        )
        draft = self._ask(prompt, EmailDraft, temperature=0.45)
        if resume_link and resume_link not in draft.body:
            draft.body = draft.body.rstrip() + "\n\nResume: " + resume_link
        return draft

    def pick_resume_for_company(self, *, company: str, role: str, context: str) -> str:
        """Choose a resume variant for outreach, where there is no job post."""
        variants = [
            {"name": r.name, "focus_keywords": r.focus_keywords} for r in self.cfg.resumes
        ]
        prompt = (
            "Pick the best resume variant to attach to a cold outreach email.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE SKILLS: "
            + ", ".join(self.cfg.profile.skills)
            + "\nTARGET ROLES: "
            + ", ".join(self.cfg.profile.target_roles)
            + "\n\nVARIANTS:\n"
            + json.dumps(variants, indent=2)
            + "\n\nCOMPANY: "
            + company
            + "\nROLE OF INTEREST: "
            + (role or "any suitable entry-level role")
            + "\nCONTEXT:\n"
            + _truncate(context, 2000)
            + "\n\nReply as JSON with a score of 100 and the chosen variant: "
            '{"match_score": 100, "best_resume": "<variant name>", '
            '"reason": "...", "red_flags": []}'
        )
        result = self._ask(prompt, MatchResult)
        known = {r.name.lower() for r in self.cfg.resumes}
        if result.best_resume.lower() not in known:
            return self.cfg.resumes[0].name
        return result.best_resume

    # ------------------------------------------------------ 6. follow-ups

    def follow_up(
        self, *, title: str, company: str, applied_on: str, channel: str = "email"
    ) -> str:
        prompt = (
            "Write a polite 3-line follow-up message about an application that has "
            "had no reply.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE: "
            + self.cfg.profile.name
            + "\nROLE: "
            + title
            + " at "
            + company
            + "\nAPPLIED ON: "
            + applied_on
            + "\nCHANNEL: "
            + channel
            + " (keep it short enough for a LinkedIn message)\n\n"
            "Exactly three lines: a reminder of the application with its date, one "
            "line of continued interest, one line asking about next steps. No "
            "pressure, no invented details.\n"
            '\nReply as JSON: {"message": "<three lines separated by \\n>"}'
        )
        return self._ask(prompt, FollowUp, temperature=0.4).message


__all__ = ["GeminiClient", "GeminiUnavailable", "GeminiInvalidJSON"]
