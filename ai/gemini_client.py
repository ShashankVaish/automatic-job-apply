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


def enforce_word_limit(text: str, limit: int, resume_link: str = "") -> str:
    """Hold a generated letter to its word limit.

    The prompt asks for under `limit` words, but a model can overshoot, and a
    cover-letter box with a 200-word essay in it is worse than a short one. We
    cut at the last complete sentence that fits and keep the resume link.
    """
    body = text
    tail = ""
    if resume_link and resume_link in text:
        head, _, _ = text.rpartition(resume_link)
        body = head.rstrip().rstrip("Resume:").rstrip()
        tail = "\n\nResume: " + resume_link

    words = body.split()
    if len(words) <= limit:
        return text.strip()

    log.info("Cover letter came back at %d words; trimming to %d", len(words), limit)
    clipped = " ".join(words[:limit])
    # Prefer ending on a sentence rather than mid-clause.
    cut = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "))
    if cut > len(clipped) // 2:
        clipped = clipped[: cut + 1]
    elif not clipped.endswith((".", "!", "?")):
        clipped = clipped.rstrip(",;: ") + "."
    return (clipped + tail).strip()


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
        letter = enforce_word_limit(letter, 150, resume_link)
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
        recipient_role: str,
        company: str,
        job_role: str,
        contact_name: str = "",
        job_description: str = "",
        company_context: str = "",
        resume_link: str = "",
        resume_variant: str = "",
    ) -> EmailDraft:
        """A cold email tailored to who is reading it.

        `recipient_role` is "hr", "founder" or "cofounder". The three get
        genuinely different emails: HR wants an application, a founder wants to
        know you understand what they are building, a co-founder wants whatever
        is relevant to their function.
        """
        role = (recipient_role or "hr").lower()
        greeting = (
            "Hi " + contact_name.split()[0]
            if contact_name.strip()
            else "Hi " + (company or "team") + " team"
        )

        if role == "hr":
            tone = (
                "AUDIENCE: an HR person or recruiter.\n"
                "- Formal and direct. This is an application, so say so in the "
                "first line: you are applying for the role and want to be "
                "considered.\n"
                "- Body length: 90 to 150 words.\n"
                "- State your year, degree and college.\n"
                "- Name one specific detail from the job post and connect it to "
                "something you have actually built.\n"
                "- Say the resume is attached and also linked.\n"
                "- Close by asking to be considered, and offer to do a task or "
                "an interview.\n"
            )
        elif role == "founder":
            tone = (
                "AUDIENCE: the founder or CEO. Their time is the scarcest thing "
                "here.\n"
                "- Shorter: 80 to 110 words. No preamble.\n"
                "- Show in one line that you understand what the company is "
                "building, using only the context given below. If the context "
                "says nothing about their product, say nothing about it.\n"
                "- Focus on how you could help them ship or grow, not on your "
                "coursework.\n"
                "- Give one concrete thing you built and its result.\n"
                "- Ask for a 10-minute call this week, or to be pointed to the "
                "right person.\n"
            )
        else:
            tone = (
                "AUDIENCE: a co-founder. Tailor to their function, which you "
                "should infer from their title in the context below: a CTO wants "
                "technical depth and projects; a COO, CMO or CPO wants execution "
                "and results.\n"
                "- Length: 80 to 110 words.\n"
                "- Give one concrete thing you built and its result, chosen to "
                "match their function.\n"
                "- Ask for a 10-minute call, or to be pointed to the right "
                "person.\n"
            )

        prompt = (
            "Write a cold outreach email from a job seeker to a specific person "
            "at a company.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE PROFILE:\n"
            + self.cfg.profile.as_prompt_block()
            + ("\nResume variant being sent: " + resume_variant if resume_variant else "")
            + "\n\nCOMPANY: "
            + company
            + "\nROLE BEING APPLIED FOR: "
            + (job_role or "any suitable entry-level role")
            + "\nRECIPIENT: "
            + (contact_name or "(name unknown)")
            + " ("
            + role
            + ")\n\nJOB POST (use this for the specific detail):\n"
            + _truncate(job_description, 5000)
            + "\n\nWHAT WE KNOW ABOUT THE COMPANY (may be empty):\n"
            + _truncate(company_context, 2500)
            + "\n\n"
            + tone
            + "\nRULES FOR EVERY EMAIL:\n"
            "- subject: fewer than 9 words, and it must contain the role name.\n"
            "- Plain text only. No markdown, no emoji, no exclamation marks.\n"
            "- No buzzwords ('synergy', 'passionate', 'rockstar', 'disrupt', "
            "'leverage'), no flattery ('huge fan', 'love what you're building').\n"
            "- Mention 2 or 3 of the candidate's most relevant real skills or "
            "projects. Include a concrete result ONLY if the profile supports "
            "one - never invent a metric, a user count or an employer.\n"
            "- Mention exactly one specific thing about the company or the role, "
            "taken from the job post or the context above. If neither contains "
            "anything specific, refer to the role itself and nothing more.\n"
            "- The greeting must be exactly: " + greeting + ",\n"
            "- Include the resume link on its own line: Resume: " + resume_link + "\n"
            "- Signature on the last line, exactly: "
            + " | ".join(
                part
                for part in (
                    self.cfg.profile.name,
                    self.cfg.profile.phone,
                    self.cfg.profile.linkedin,
                )
                if part
            )
            + "\n"
            '\nReply as JSON: {"subject": "...", "body": "..."}'
        )
        draft = self._ask(prompt, EmailDraft, temperature=0.5)
        draft.body = self._finish_email_body(draft.body, resume_link)
        return draft

    def outreach_followup(
        self,
        *,
        company: str,
        job_role: str,
        contact_name: str = "",
        first_contacted: str = "",
        original_subject: str = "",
        resume_link: str = "",
    ) -> EmailDraft:
        """The single follow-up, sent once to the HR contact only.

        There is never a second one, so this must not read as the start of a
        sequence, and must not imply they were rude not to reply.
        """
        greeting = (
            "Hi " + contact_name.split()[0]
            if contact_name.strip()
            else "Hi " + (company or "team") + " team"
        )
        prompt = (
            "Write ONE short follow-up to a job application email that received "
            "no reply. This is the only follow-up that will ever be sent.\n\n"
            + HONESTY_RULE
            + "\nCANDIDATE: "
            + self.cfg.profile.name
            + "\nCOMPANY: "
            + company
            + "\nROLE: "
            + (job_role or "the role")
            + "\nFIRST EMAIL SENT: "
            + (first_contacted or "about a week ago")
            + "\nORIGINAL SUBJECT: "
            + (original_subject or "")
            + "\n\nRequirements:\n"
            "- 3 or 4 short lines total. This is a reply in the same thread, so "
            "do not reintroduce the candidate at length.\n"
            "- Line 1: a one-line reminder of the application and roughly when "
            "it was sent.\n"
            "- Line 2: that they are still interested, and one short truthful "
            "detail from the profile.\n"
            "- Line 3: a light ask about next steps, and make clear this is the "
            "last time you'll follow up.\n"
            "- No pressure, no guilt, no 'bumping this to the top of your "
            "inbox', no implication they did anything wrong.\n"
            "- subject: reply-style. Use 'Re: " + (original_subject or "your role")
            + "'.\n"
            "- The greeting must be exactly: " + greeting + ",\n"
            "- Include the resume link on its own line: Resume: " + resume_link + "\n"
            '\nReply as JSON: {"subject": "...", "body": "..."}'
        )
        draft = self._ask(prompt, EmailDraft, temperature=0.45)
        draft.body = self._finish_email_body(draft.body, resume_link)
        return draft

    def _finish_email_body(self, body: str, resume_link: str) -> str:
        """Guarantee the resume link and the signature are present."""
        text = (body or "").rstrip()
        if resume_link and resume_link not in text:
            text += "\n\nResume: " + resume_link
        signature = " | ".join(
            part
            for part in (
                self.cfg.profile.name,
                self.cfg.profile.phone,
                self.cfg.profile.linkedin,
            )
            if part
        )
        if signature and self.cfg.profile.name not in text:
            text += "\n\n" + signature
        return text

    def pick_resume_for_company(self, *, company: str, role: str, context: str) -> str:
        """Choose a resume variant when there is no scored job to go on."""
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
