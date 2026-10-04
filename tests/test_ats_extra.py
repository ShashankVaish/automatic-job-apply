"""Stage 4: Ashby, SmartRecruiters, Workday and the generic career-page filler."""
from __future__ import annotations

import pytest

from ats.ashby import AshbyFiller
from ats.detect import detect, is_known_ats_host
from ats.fields import FillContext
from ats.generic import GenericFiller
from ats.smartrecruiters import SmartRecruitersFiller
from ats.workday import WorkdayFiller
from models import ApplyTarget, FillReport, Job
from tests.conftest import fixture_url


def make_ctx(cfg, gemini, job, variant="general"):
    return FillContext(
        cfg=cfg, job=job, resume=cfg.resume(variant), gemini=gemini,
        report=FillReport(), pacer=None,
    )


def job_for(page, title="Role", company="Fixture"):
    return Job(
        site="career_pages",
        title=title,
        company=company,
        url=page.url,
        description="React, Python and SQL. 0-1 years of experience.",
        apply_target=ApplyTarget.CAREER_PAGE,
    )


# ------------------------------------------------------------- detection


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jobs.ashbyhq.com/fixture/abc-123", "ashby"),
        ("https://jobs.smartrecruiters.com/Fixture/744000", "smartrecruiters"),
        ("https://fixture.wd3.myworkdayjobs.com/en-US/careers/job/x", "workday"),
        ("https://fixture.myworkdaysite.com/recruiting/x", "workday"),
        ("https://careers.fixture.example/openings/12", "generic"),
    ],
)
def test_detect_from_url(url, expected):
    assert detect(url).name == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/x/jobs/1",
        "https://jobs.lever.co/x/y",
        "https://jobs.ashbyhq.com/x/y",
        "https://x.myworkdayjobs.com/y",
        "https://jobs.smartrecruiters.com/x/1",
        "https://apply.workable.com/x/j/1",
    ],
)
def test_known_ats_hosts_are_recognised(url):
    assert is_known_ats_host(url) is True


def test_a_company_site_is_not_a_known_ats_host():
    assert is_known_ats_host("https://careers.fixture.example/apply") is False


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("ashby.html", "ashby"),
        ("smartrecruiters.html", "smartrecruiters"),
        ("workday_account_wall.html", "workday"),
        ("generic_career_page.html", "generic"),
    ],
)
def test_detect_from_markup(page, fixture, expected):
    page.goto(fixture_url(fixture))
    assert detect(page.url, page).name == expected


# ----------------------------------------------------------------- Ashby


def test_ashby_fills_form_and_listbox(page, cfg, gemini):
    page.goto(fixture_url("ashby.html"))
    filler = AshbyFiller(page)
    assert filler.open_form() is True

    job = job_for(page, "Software Engineer Intern", "Fixture Labs")
    ctx = make_ctx(cfg, gemini, job)
    filler.fill(ctx)

    p = cfg.profile
    assert page.input_value("#a-name") == p.name
    assert page.input_value("#a-email") == p.email
    assert page.input_value("#a-phone") == p.phone
    assert page.input_value("#a-linkedin") == p.linkedin
    assert page.input_value("#a-github") == p.github
    assert ctx.report.resume_uploaded is True
    # The React-style listbox is not a <select>, so it needs its own handling.
    assert page.inner_text("#auth-listbox").strip() == "Yes"
    # "Why do you want to work here?" is a cover letter in disguise.
    assert len(page.input_value("#a-why")) > 20


# -------------------------------------------------------- SmartRecruiters


def test_smartrecruiters_fills_known_ids_and_consent(page, cfg, gemini):
    page.goto(fixture_url("smartrecruiters.html"))
    filler = SmartRecruitersFiller(page)
    assert filler.open_form() is True

    job = job_for(page, "Graduate Engineer", "Fixture Global")
    ctx = make_ctx(cfg, gemini, job)
    filler.fill(ctx)

    p = cfg.profile
    assert page.input_value("#firstName") == p.first_name()
    assert page.input_value("#lastName") == p.last_name()
    assert page.input_value("#email") == p.email
    assert page.input_value("#phoneNumber") == p.phone
    assert page.input_value("#location") == p.location
    assert page.input_value("#linkedinProfileUrl") == p.linkedin
    assert ctx.report.resume_uploaded is True
    assert len(page.input_value("#sr-cover")) > 20
    # Required consent box must be ticked or the form can never be submitted.
    assert page.is_checked("#consent") is True


# --------------------------------------------------------------- Workday


def test_workday_refuses_the_account_wall(page, cfg, gemini):
    """Workday wants an account per employer. We never create one or type a
    password, so this must come back as "no form" rather than half-filled."""
    page.goto(fixture_url("workday_account_wall.html"))
    filler = WorkdayFiller(page)

    assert filler.needs_account() is True
    assert filler.open_form() is False
    # "Next" must never be mistaken for a submit button.
    assert filler.submit_locator() is None
    assert filler.has_next_button() is True


def test_workday_never_types_into_the_password_box(page, cfg, gemini):
    page.goto(fixture_url("workday_account_wall.html"))
    filler = WorkdayFiller(page)
    job = job_for(page, "Associate Software Engineer", "Fixture Corp")
    ctx = make_ctx(cfg, gemini, job)
    filler.fill(ctx)

    assert page.input_value("#wd-pass") == ""
    assert ctx.report.needs_review is True
    assert any("account" in r for r in ctx.report.review_reasons)


# --------------------------------------------------------------- generic


def test_generic_filler_opens_a_form_behind_an_apply_button(page, cfg, gemini):
    page.goto(fixture_url("generic_career_page.html"))
    filler = GenericFiller(page)

    assert filler.has_form() is False, "the form starts hidden behind Apply now"
    assert filler.open_form() is True

    job = job_for(page, "Junior Developer", "Fixture Startup")
    ctx = make_ctx(cfg, gemini, job)
    filler.fill(ctx)

    p = cfg.profile
    assert page.input_value("#g-name") == p.name
    assert page.input_value("#g-email") == p.email
    assert page.input_value("#g-phone") == p.phone
    assert page.input_value("#g-notice") == p.notice_period
    assert page.input_value("#g-exp") == str(p.years_of_experience)
    assert ctx.report.resume_uploaded is True
    # "Link to your resume" is a resume-link field, so it gets the Drive link.
    assert page.input_value("#g-link") == cfg.resume("general").public_link
    assert ctx.report.resume_link_placed is True
    # "Anything else you'd like us to know?" carries the link too.
    assert cfg.resume("general").public_link in page.input_value("#g-extra")


def test_generic_filler_flags_a_page_with_nothing_to_fill(page, cfg, gemini):
    page.set_content("<html><body><h1>We have no openings</h1></body></html>")
    filler = GenericFiller(page)
    assert filler.open_form() is False

    job = job_for(page)
    ctx = make_ctx(cfg, gemini, job)
    filler.fill(ctx)
    assert ctx.report.needs_review is True
    assert any("nothing could be filled" in r for r in ctx.report.review_reasons)


def test_generic_filler_finds_the_send_button(page, cfg, gemini):
    page.goto(fixture_url("generic_career_page.html"))
    filler = GenericFiller(page)
    filler.open_form()
    submit = filler.submit_locator()
    assert submit is not None
    assert "Send application" in submit.inner_text()


# ------------------------------------------------- confirmation detection


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Thank you for applying to Fixture Labs!", True),
        ("Your application has been submitted.", True),
        ("We have received your application", True),
        ("Please complete the form below", False),
    ],
)
def test_confirmation_pages_are_recognised(page, text, expected):
    page.set_content("<html><body><p>" + text + "</p></body></html>")
    assert GenericFiller(page).confirmed() is expected
