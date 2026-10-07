"""The prompts sent to Gemini.

These are the actual product for anything the model writes, and the only part
of email quality that can be checked without an API key. The tests capture the
prompt text and assert each spec constraint is really in it, so a later edit
can't quietly drop "under 150 words" or the honesty rule.
"""
from __future__ import annotations

import json

import pytest

from ai.gemini_client import GeminiClient, enforce_word_limit
from ai.schemas import AnswerResult, CoverLetter, EmailDraft, MatchResult


@pytest.fixture
def captured(cfg, monkeypatch):
    """Capture the prompt instead of calling Gemini."""
    client = GeminiClient(cfg)
    seen: dict[str, str] = {}

    def fake_ask(prompt, schema, temperature=0.2):
        seen["prompt"] = prompt
        seen["schema"] = schema.__name__
        seen["temperature"] = temperature
        if schema is EmailDraft:
            return EmailDraft(subject="Subject here", body="Body here")
        if schema is CoverLetter:
            return CoverLetter(text="Letter here")
        if schema is MatchResult:
            return MatchResult(match_score=80, best_resume=cfg.resumes[0].name)
        if schema is AnswerResult:
            return AnswerResult(kind="text", value="answer")
        raise AssertionError("unexpected schema " + schema.__name__)

    monkeypatch.setattr(client, "_ask", fake_ask)
    return client, seen


# ------------------------------------------------------- the honesty rule


@pytest.mark.parametrize(
    "call",
    [
        "score_match",
        "answer_question",
        "cover_letter",
        "outreach_email",
        "outreach_followup",
    ],
)
def test_every_prompt_carries_the_honesty_rule(captured, cfg, call):
    """The one rule that must never be missing from any prompt."""
    client, seen = captured

    if call == "score_match":
        client.score_match(title="T", company="C", location="L", description="D")
    elif call == "answer_question":
        client.answer_question(
            question="Q", field_kind="text", options=[], job_title="T",
            company="C", description="D",
        )
    elif call == "cover_letter":
        client.cover_letter(title="T", company="C", description="D", resume_link="L")
    elif call == "outreach_email":
        client.outreach_email(
            recipient_role="hr", company="C", job_role="R", resume_link="L"
        )
    else:
        client.outreach_followup(company="C", job_role="R", resume_link="L")

    prompt = seen["prompt"]
    assert "Never invent or exaggerate experience" in prompt
    assert "set confident=false" in prompt or "honest answer" in prompt
    assert "JSON only" in prompt or "Reply as JSON" in prompt


def test_prompts_include_the_real_profile_and_nothing_invented(captured, cfg):
    client, seen = captured
    client.outreach_email(
        recipient_role="hr", company="Fixture Labs", job_role="Intern", resume_link="L"
    )
    prompt = seen["prompt"]

    assert cfg.profile.name in prompt
    assert cfg.profile.degree in prompt
    assert cfg.profile.college in prompt
    for skill in cfg.profile.skills:
        assert skill in prompt
    assert str(cfg.profile.years_of_experience) in prompt


# ----------------------------------------------- 8.3 the email constraints


def test_hr_prompt_asks_for_a_formal_application(captured):
    client, seen = captured
    client.outreach_email(
        recipient_role="hr", company="Fixture Labs", job_role="Frontend Intern",
        contact_name="Priya Nair", resume_link="https://drive.example/r",
    )
    prompt = seen["prompt"]

    assert "HR person or recruiter" in prompt
    assert "90 to 150 words" in prompt
    assert "considered" in prompt
    assert "Hi Priya," in prompt, "the greeting must be specified exactly"


def test_founder_prompt_asks_for_a_shorter_email_and_a_call(captured):
    client, seen = captured
    client.outreach_email(
        recipient_role="founder", company="Fixture Labs", job_role="Frontend Intern",
        contact_name="Arjun Mehta", resume_link="https://drive.example/r",
    )
    prompt = seen["prompt"]

    assert "80 to 110 words" in prompt
    assert "10-minute call" in prompt
    assert "right person" in prompt
    assert "founder or CEO" in prompt


def test_cofounder_prompt_asks_for_their_function(captured):
    client, seen = captured
    client.outreach_email(
        recipient_role="cofounder", company="Fixture Labs", job_role="Intern",
        contact_name="Divya Shah", resume_link="https://drive.example/r",
    )
    prompt = seen["prompt"]

    assert "co-founder" in prompt
    assert "CTO" in prompt
    assert "COO" in prompt
    assert "80 to 110 words" in prompt


@pytest.mark.parametrize("role", ["hr", "founder", "cofounder"])
def test_every_outreach_prompt_sets_the_shared_rules(captured, cfg, role):
    client, seen = captured
    client.outreach_email(
        recipient_role=role, company="Fixture Labs", job_role="Frontend Intern",
        resume_link="https://drive.example/r",
    )
    prompt = seen["prompt"]

    assert "fewer than 9 words" in prompt, "spec: subject under 9 words"
    assert "contain the role name" in prompt
    assert "no emoji" in prompt
    assert "buzzwords" in prompt
    assert "flattery" in prompt
    assert "never invent a metric" in prompt
    assert "exactly one specific thing about the company" in prompt
    assert "https://drive.example/r" in prompt
    # The signature the spec asks for: name, phone, LinkedIn.
    assert cfg.profile.phone in prompt
    assert cfg.profile.linkedin in prompt


def test_the_greeting_falls_back_to_the_company_team(captured):
    client, seen = captured
    client.outreach_email(
        recipient_role="hr", company="Fixture Labs", job_role="Intern",
        contact_name="", resume_link="L",
    )
    assert "Hi Fixture Labs team," in seen["prompt"]


def test_the_job_description_is_given_for_the_specific_detail(captured):
    client, seen = captured
    client.outreach_email(
        recipient_role="hr", company="C", job_role="R",
        job_description="We use React and ship every Friday.", resume_link="L",
    )
    assert "We use React and ship every Friday." in seen["prompt"]


def test_an_empty_company_context_is_not_to_be_invented(captured):
    client, seen = captured
    client.outreach_email(
        recipient_role="founder", company="C", job_role="R",
        company_context="", resume_link="L",
    )
    prompt = seen["prompt"]
    # The founder prompt forbids inventing product knowledge, and the shared
    # rules give a fallback when nothing specific is available.
    assert "says nothing about their product, say nothing about it" in prompt
    assert "refer to the role itself and nothing more" in prompt


# ------------------------------------------------- 8.5 the one follow-up


def test_the_followup_prompt_says_it_is_the_only_one(captured):
    client, seen = captured
    client.outreach_followup(
        company="Fixture Labs", job_role="Frontend Intern",
        contact_name="Priya Nair", first_contacted="2026-10-01",
        original_subject="Application - Frontend Intern", resume_link="L",
    )
    prompt = seen["prompt"]

    assert "only follow-up that will ever be sent" in prompt
    assert "3 or 4 short lines" in prompt
    assert "last time" in prompt
    assert "no guilt" in prompt
    assert "Re: Application - Frontend Intern" in prompt
    assert "2026-10-01" in prompt


# --------------------------------------------------- the cover letter


def test_the_cover_letter_prompt_states_the_word_limit(captured):
    client, seen = captured
    client.cover_letter(
        title="Frontend Intern", company="Fixture Labs",
        description="React work", resume_link="https://drive.example/r",
    )
    prompt = seen["prompt"]

    assert "under 150 words" in prompt
    assert "no placeholders" in prompt
    assert "Resume: https://drive.example/r" in prompt


def test_the_cover_letter_limit_is_enforced_as_well_as_asked_for():
    link = "https://drive.example/r"
    long_letter = " ".join(["Sentence {0} here.".format(i) for i in range(90)])
    trimmed = enforce_word_limit(long_letter + "\n\nResume: " + link, 150, link)
    body = trimmed.split("Resume:")[0]
    assert len(body.split()) <= 150
    assert link in trimmed


# ----------------------------------------------- scoring and questions


def test_the_scoring_prompt_lists_every_resume_variant(captured, cfg):
    client, seen = captured
    client.score_match(
        title="Frontend Intern", company="Fixture Labs", location="Noida",
        description="React work",
    )
    prompt = seen["prompt"]

    for variant in cfg.resumes:
        assert variant.name in prompt
        for keyword in variant.focus_keywords:
            assert keyword in prompt
    assert "Penalise heavily" in prompt
    assert "more years of experience than the candidate has" in prompt


def test_the_question_prompt_pins_answers_to_the_allowed_options(captured):
    client, seen = captured
    client.answer_question(
        question="Are you authorized to work in India?",
        field_kind="select",
        options=["Yes", "No"],
        job_title="T",
        company="C",
        description="D",
    )
    prompt = seen["prompt"]

    assert json.dumps(["Yes", "No"]) in prompt
    assert "character-for-character" in prompt
    assert "confident=false" in prompt


def test_number_fields_are_told_to_return_digits(captured):
    client, seen = captured
    client.answer_question(
        question="Years of experience?", field_kind="number", options=[],
        job_title="T", company="C", description="D",
    )
    assert "digits only" in seen["prompt"]


# -------------------------------------------------------- temperatures


def test_factual_calls_run_cooler_than_creative_ones(captured):
    """Scoring and screening answers should not be imaginative."""
    client, seen = captured

    client.score_match(title="T", company="C", location="L", description="D")
    scoring = seen["temperature"]

    client.outreach_email(
        recipient_role="hr", company="C", job_role="R", resume_link="L"
    )
    writing = seen["temperature"]

    assert scoring <= 0.2
    assert writing > scoring
