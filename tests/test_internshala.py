"""Stage 2: Internshala scraping, routing and its in-site apply modal.

Everything runs against local fixtures; no request ever leaves the machine.
"""
from __future__ import annotations

import pytest

from ats.fields import FillContext
from models import ApplyTarget, FillReport, Job
from sites.internshala import Internshala
from tests.conftest import fixture_url


@pytest.fixture
def source(cfg, page, gemini, db, pacer):
    return Internshala(cfg, page, gemini=gemini, db=db, pacer=pacer)


# -------------------------------------------------------------- search URLs


def test_search_url_for_work_from_home(source):
    url = source.search_url("web development", "work-from-home")
    assert url.startswith("https://internshala.com/internships/")
    assert "work-from-home-internships" in url
    assert "keywords-web" in url


def test_search_url_for_a_city(source):
    url = source.search_url("python", "Delhi NCR")
    assert "internships-in-delhi-ncr" in url
    assert "keywords-python" in url


def test_search_url_with_no_inputs(source):
    assert source.search_url("", "") == "https://internshala.com/internships/internships/"


# ----------------------------------------------------------- reading cards


def test_reads_all_result_cards(source, page):
    page.goto(fixture_url("internshala_search.html"))
    jobs = source._cards()
    assert len(jobs) == 3

    first = jobs[0]
    assert first.title == "Frontend Development Internship"
    assert first.company == "Fixture Web Studio"
    assert first.location == "Work From Home"
    assert "15,000" in first.stipend
    assert first.posted == "Posted 2 days ago"
    assert first.url.endswith("internshala_detail.html")
    assert first.site == "internshala"


def test_missing_cards_do_not_raise(source, page):
    page.set_content("<html><body><p>Nothing here</p></body></html>")
    assert source._cards() == []


# ----------------------------------------------------- details and routing


def test_details_of_an_in_site_posting(source, page):
    job = Job(site="internshala", title="", company="", url=fixture_url("internshala_detail.html"))
    job = source.get_job_details(job)

    assert job.title == "Frontend Development Internship"
    assert job.company == "Fixture Web Studio"
    assert "React" in job.description
    assert job.posted == "Posted 2 days ago"
    # No external link and no apply email, so Internshala's own flow is used.
    assert job.apply_target == ApplyTarget.BOARD
    assert job.external_url == ""
    assert job.apply_email == ""


def test_routes_to_an_external_career_page(source, page):
    job = Job(
        site="internshala", title="", company="",
        url=fixture_url("internshala_detail_external.html"),
    )
    job = source.get_job_details(job)

    assert job.apply_target == ApplyTarget.CAREER_PAGE
    # The fixture's "Apply on company website" link is followed, and the
    # relative href resolves against the page we're on - not internshala.com.
    assert job.external_url.endswith("greenhouse.html")
    assert "internshala.com" not in job.external_url
    assert job.apply_url() == job.external_url


def test_known_ats_hosts_are_identified_before_opening_them(source):
    """route() labels the ATS from the URL alone, with no page load."""
    job = Job(
        site="internshala",
        title="Software Engineer Intern",
        company="Fixture Labs",
        url="https://internshala.com/internship/detail/x",
        description="A normal description with no apply email.",
        external_url="https://job-boards.greenhouse.io/fixturelabs/jobs/4001",
    )
    routed = source.route(job)
    assert routed.apply_target == ApplyTarget.CAREER_PAGE
    assert routed.ats == "greenhouse"

    job.external_url = "https://jobs.lever.co/fixture/abc"
    job.ats = ""
    assert source.route(job).ats == "lever"


def test_routes_to_email_when_the_posting_says_to_email(source, page):
    job = Job(
        site="internshala", title="", company="",
        url=fixture_url("internshala_detail_email.html"),
    )
    job = source.get_job_details(job)

    assert job.apply_target == ApplyTarget.EMAIL
    assert job.apply_email == "careers@fixturemedia.example"


def test_noreply_addresses_are_never_used_for_applications(source):
    text = "Send your resume to noreply@example.com for consideration."
    assert source.find_apply_email(text) == ""


def test_a_bare_email_in_the_text_is_not_an_apply_route(source):
    text = "Our office manager is reachable at office@example.com during the week."
    assert source.find_apply_email(text) == ""


def test_apply_email_is_found_with_various_wordings(source):
    assert (
        source.find_apply_email("Please mail your CV to jobs@example.com")
        == "jobs@example.com"
    )
    assert (
        source.find_apply_email("Apply via email: talent@example.com")
        == "talent@example.com"
    )


# ------------------------------------------------------- in-site apply flow


def test_fills_the_internshala_apply_modal(source, page, cfg, gemini):
    job = Job(
        site="internshala",
        title="Frontend Development Internship",
        company="Fixture Web Studio",
        url=fixture_url("internshala_detail.html"),
        description="React and JavaScript internship, work from home.",
        apply_target=ApplyTarget.BOARD,
    )
    assert source.goto(job.url)

    report = FillReport()
    ctx = FillContext(
        cfg=cfg, job=job, resume=cfg.resume("frontend"), gemini=gemini,
        report=report, pacer=pacer_none(),
    )
    outcome = source.apply(job, ctx)

    # The modal opened and the questions were answered.
    assert page.locator("#application_form").is_visible()
    assert page.input_value("#q3") == cfg.profile.phone
    assert page.input_value("#q2") == "Yes"
    assert len(page.input_value("#q1")) > 20, "the 'why hire you' box should be written"
    # Internshala's own flow never self-submits; it hands back to the runner.
    assert outcome.status in ("filled", "needs_review")


def test_apply_reports_when_the_modal_cannot_be_opened(source, page, cfg, gemini):
    page.set_content("<html><body><p>No apply button here</p></body></html>")
    job = Job(site="internshala", title="X", company="Y", url=page.url)
    ctx = FillContext(
        cfg=cfg, job=job, resume=cfg.resume("general"), gemini=gemini,
        report=FillReport(), pacer=pacer_none(),
    )
    outcome = source.apply(job, ctx)
    assert outcome.status == "needs_review"
    assert "could not open" in outcome.reason


def pacer_none():
    from browser.human import Pacer

    return Pacer(0, 0, 0, 0, enabled=False)
