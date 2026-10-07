"""Stage 2: Greenhouse and Lever filled end to end against local fixtures.

Nothing is ever submitted here - fillers can't submit by design, the runner
owns that decision.
"""
from __future__ import annotations

import pytest

from ats.detect import detect
from ats.fields import FillContext
from ats.greenhouse import GreenhouseFiller
from ats.lever import LeverFiller
from models import ApplyTarget, FillReport, Job
from tests.conftest import fixture_url


def make_ctx(cfg, gemini, job, variant="frontend"):
    report = FillReport()
    return FillContext(
        cfg=cfg,
        job=job,
        resume=cfg.resume(variant),
        gemini=gemini,
        report=report,
        pacer=None,
    )


def field_map(report: FillReport) -> dict[str, str]:
    return dict(report.fields)


def answer_map(report: FillReport) -> dict[str, str]:
    return {q: a for q, a in report.questions}


# --------------------------------------------------------------- detection


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/9", "greenhouse"),
        ("https://grnh.se/abc123", "greenhouse"),
        ("https://jobs.lever.co/acme/uuid", "lever"),
        ("https://jobs.eu.lever.co/acme/uuid", "lever"),
        ("https://careers.example.com/apply/42", "generic"),
    ],
)
def test_detect_from_url(url, expected):
    assert detect(url).name == expected


def test_detect_greenhouse_from_markup(page):
    page.goto(fixture_url("greenhouse.html"))
    # The fixture is served from file://, so the URL can't give it away.
    assert detect(page.url, page).name == "greenhouse"


def test_detect_lever_from_markup(page):
    page.goto(fixture_url("lever.html"))
    assert detect(page.url, page).name == "lever"


# -------------------------------------------------------------- greenhouse


@pytest.fixture
def greenhouse_filled(page, cfg, gemini):
    page.goto(fixture_url("greenhouse.html"))
    job = Job(
        site="career_pages",
        title="Frontend Engineer Intern",
        company="Fixture Labs",
        url=page.url,
        location="Noida, India",
        description="React, JavaScript and CSS internship. 0-1 years of experience.",
        apply_target=ApplyTarget.CAREER_PAGE,
    )
    filler = GreenhouseFiller(page)
    assert filler.open_form() is True
    ctx = make_ctx(cfg, gemini, job)
    filler.fill(ctx)
    return page, filler, ctx


def test_greenhouse_fills_standard_fields(greenhouse_filled, cfg):
    page, _, ctx = greenhouse_filled
    p = cfg.profile

    assert page.input_value("#first_name") == p.first_name()
    assert page.input_value("#last_name") == p.last_name()
    assert page.input_value("#email") == p.email
    assert page.input_value("#phone") == p.phone
    assert page.input_value("#q_linkedin") == p.linkedin
    assert page.input_value("#q_github") == p.github
    assert page.input_value("#q_notice") == p.notice_period
    assert page.input_value("#q_college") == p.college
    assert page.input_value("#q_grad") == str(p.graduation_year)

    filled = field_map(ctx.report)
    assert filled["first_name"] == p.first_name()
    assert filled["email"] == p.email


def test_greenhouse_uploads_resume_and_skips_photo(greenhouse_filled, cfg):
    page, _, ctx = greenhouse_filled
    assert ctx.report.resume_uploaded is True

    names = page.evaluate(
        "() => [...document.querySelectorAll(\"input[type=file]\")]"
        ".map(i => [i.id, i.files.length ? i.files[0].name : ''])"
    )
    by_id = dict(names)
    assert by_id["resume"].endswith(".pdf"), "resume slot should have the PDF"
    assert by_id["photo"] == "", "a photo slot must never get the resume"


def test_greenhouse_places_resume_link(greenhouse_filled, cfg):
    """A field labelled "Website or Portfolio" gets the configured portfolio.
    The resume link still has to reach the form - here via the cover letter."""
    page, _, ctx = greenhouse_filled
    link = cfg.resume("frontend").public_link
    assert page.input_value("#q_website") == cfg.profile.portfolio
    assert ctx.report.resume_link_placed is True
    assert link in page.input_value("#cover_letter_text")


def test_greenhouse_writes_cover_letter(greenhouse_filled, cfg):
    page, _, ctx = greenhouse_filled
    assert ctx.report.cover_letter is True
    text = page.input_value("#cover_letter_text")
    assert "Fixture Labs" in text
    assert cfg.resume("frontend").public_link in text


def test_greenhouse_never_touches_password(greenhouse_filled):
    page, _, _ = greenhouse_filled
    assert page.input_value("#acct_password") == ""


def test_greenhouse_answers_number_question_honestly(greenhouse_filled, cfg):
    page, _, _ = greenhouse_filled
    # The profile says 0 years. It must not be inflated.
    assert page.input_value("#q_years") == str(cfg.profile.years_of_experience)


def test_greenhouse_answers_dropdowns(greenhouse_filled):
    page, _, _ = greenhouse_filled
    assert page.input_value("#q_auth") == "Yes"
    assert page.input_value("#q_sponsor") == "No"


def test_greenhouse_answers_radio_and_checkbox(greenhouse_filled):
    page, _, ctx = greenhouse_filled
    checked = page.evaluate(
        "() => [...document.querySelectorAll(\"input[name=relocate]\")]"
        ".filter(i => i.checked).map(i => i.value)"
    )
    assert checked == ["yes"], "profile says willing to relocate"

    skills = page.evaluate(
        "() => [...document.querySelectorAll(\"input[name='skills[]']\")]"
        ".filter(i => i.checked).map(i => i.value)"
    )
    # React and SQL are in the profile; Vue and Rust are not and must stay off.
    assert "react" in skills
    assert "sql" in skills
    assert "vue" not in skills
    assert "rust" not in skills


def test_greenhouse_finds_submit_but_does_not_click(greenhouse_filled):
    page, filler, _ = greenhouse_filled
    submit = filler.submit_locator()
    assert submit is not None
    assert "Submit" in submit.inner_text()
    # Still on the form: filling never navigates.
    assert page.locator("#application-form").count() == 1


def test_greenhouse_reports_the_unanswerable_question(greenhouse_filled):
    """The mock has no rule for "favourite CSS property", so it must be flagged
    rather than silently invented."""
    _, _, ctx = greenhouse_filled
    assert ctx.report.needs_review is True
    joined = " ".join(ctx.report.review_reasons).lower()
    assert "css" in joined or "unanswered" in joined


# -------------------------------------------------------------------- lever


@pytest.fixture
def lever_filled(page, cfg, gemini):
    page.goto(fixture_url("lever.html"))
    job = Job(
        site="career_pages",
        title="Data Analyst Intern",
        company="Fixture Analytics",
        url=page.url,
        location="Remote",
        description="SQL, Python and pandas internship for a final-year student.",
        apply_target=ApplyTarget.CAREER_PAGE,
    )
    filler = LeverFiller(page)
    assert filler.open_form() is True
    ctx = make_ctx(cfg, gemini, job, variant="data")
    filler.fill(ctx)
    return page, filler, ctx


def test_lever_fills_known_named_fields(lever_filled, cfg):
    page, _, _ = lever_filled
    p = cfg.profile
    assert page.input_value("input[name='name']") == p.name
    assert page.input_value("input[name='email']") == p.email
    assert page.input_value("input[name='phone']") == p.phone
    assert page.input_value("input[name='urls[LinkedIn]']") == p.linkedin
    assert page.input_value("input[name='urls[GitHub]']") == p.github


def test_lever_places_resume_pdf_and_link(lever_filled, cfg):
    page, _, ctx = lever_filled
    link = cfg.resume("data").public_link
    assert ctx.report.resume_uploaded is True
    assert ctx.report.resume_link_placed is True
    assert page.input_value("input[name='urls[Other]']") == link


def test_lever_answers_custom_questions(lever_filled, cfg):
    page, _, ctx = lever_filled
    sql = page.evaluate(
        "() => [...document.querySelectorAll(\"input[name='cards[sql][field0]']\")]"
        ".filter(i => i.checked).map(i => i.value)"
    )
    assert sql == ["Yes"]
    assert page.input_value("select[name='cards[start][field0]']") == "Immediately"
    stipend = page.input_value("input[name='cards[stipend][field0]']")
    assert stipend == cfg.profile.expected_salary or stipend == cfg.profile.expected_stipend


def test_lever_submit_found_and_not_clicked(lever_filled):
    page, filler, _ = lever_filled
    assert filler.submit_locator() is not None
    assert page.locator("form.application-form").count() == 1


def test_lever_is_not_multistep(lever_filled):
    _, filler, _ = lever_filled
    assert filler.has_next_button() is False


# --------------------------------------- pay fields and employment history


@pytest.mark.parametrize(
    "label,expected_kind",
    [
        ("Expected monthly stipend (INR)", "stipend"),
        ("Stipend expectation", "stipend"),
        ("Expected pay per month", "stipend"),
        ("Monthly salary expectation", "stipend"),
        ("Expected CTC", "salary"),
        ("Expected salary", "salary"),
        ("Expected compensation", "salary"),
    ],
)
def test_stipend_and_salary_fields_are_told_apart(label, expected_kind):
    """A monthly stipend box will not accept an annual CTC figure."""
    from ats.fields import classify

    assert classify(label, {}) == expected_kind


def test_a_monthly_field_gets_the_stipend_not_the_salary(cfg, gemini):
    from ats.fields import FillContext, profile_value
    from models import FillReport, Job

    ctx = FillContext(
        cfg=cfg,
        job=Job(site="x", title="t", company="c", url="u"),
        resume=cfg.resume("frontend"),
        gemini=gemini,
        report=FillReport(),
        pacer=None,
    )
    assert profile_value("stipend", ctx) == cfg.profile.expected_stipend
    assert profile_value("salary", ctx) == cfg.profile.expected_salary
    assert profile_value("stipend", ctx) != profile_value("salary", ctx)


def test_lever_leaves_current_company_blank_for_a_fresher(lever_filled, cfg):
    """The college is not a current employer, and saying so would be untrue."""
    page, _, ctx = lever_filled
    assert cfg.profile.experience_level == "fresher"
    assert page.input_value("input[name='org']") == ""


def test_current_employer_is_filled_once_there_is_one(cfg, gemini):
    from ats.fields import FillContext
    from ats.lever import current_employer
    from models import FillReport, Job

    def ctx_for(level: str, years: int):
        cfg.profile.experience_level = level
        cfg.profile.years_of_experience = years
        return FillContext(
            cfg=cfg,
            job=Job(site="x", title="t", company="c", url="u"),
            resume=cfg.resume("general"),
            gemini=gemini,
            report=FillReport(),
            pacer=None,
        )

    assert current_employer(ctx_for("fresher", 0)) == ""
    assert current_employer(ctx_for("student", 0)) == ""
    assert current_employer(ctx_for("0-1 years", 0)) == ""
    assert current_employer(ctx_for("1-2 years", 2)) == cfg.profile.college
