"""Stage 4: LinkedIn, Naukri and Indeed discovery and routing.

The whole point of these modules is the routing rule: prefer the company's own
career page, use the board's flow only when there is no external page, and
never let auto mode near a board.
"""
from __future__ import annotations

import pytest

from ats.fields import FillContext
from models import ApplyTarget, FillReport, Job
from sites.indeed import Indeed
from sites.linkedin import LinkedIn
from sites.naukri import Naukri
from tests.conftest import fixture_url


def make_source(cls, cfg, page, gemini, db, pacer):
    return cls(cfg, page, gemini=gemini, db=db, pacer=pacer)


def make_ctx(cfg, gemini, job, variant="frontend"):
    return FillContext(
        cfg=cfg, job=job, resume=cfg.resume(variant), gemini=gemini,
        report=FillReport(), pacer=None,
    )


# ============================================================== LinkedIn


@pytest.fixture
def linkedin(cfg, page, gemini, db, pacer):
    return make_source(LinkedIn, cfg, page, gemini, db, pacer)


def test_linkedin_search_url_includes_filters(linkedin):
    url = linkedin.search_url("Frontend Developer Intern", "Noida, India")
    assert "keywords=Frontend+Developer+Intern" in url
    assert "location=Noida%2C+India" in url
    assert "f_TPR=r604800" in url  # config says "week"


def test_linkedin_search_url_paginates(linkedin):
    assert "start=25" in linkedin.search_url("x", "y", start=25)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "https://www.linkedin.com/jobs/view/3901/?refId=abc&trackingId=xyz",
            "https://www.linkedin.com/jobs/view/3901/",
        ),
        (
            "https://www.linkedin.com/jobs/view/4002",
            "https://www.linkedin.com/jobs/view/4002/",
        ),
    ],
)
def test_linkedin_urls_are_canonicalised_for_dedupe(raw, expected):
    assert LinkedIn.canonical_job_url(raw) == expected


def test_linkedin_reads_search_cards(linkedin, page):
    page.goto(fixture_url("linkedin_search.html"))
    jobs = linkedin._cards()

    assert len(jobs) == 2
    assert jobs[0].title == "Frontend Engineer Intern"
    assert jobs[0].company == "Fixture Labs"
    assert "Noida" in jobs[0].location
    # Tracking params stripped, so the same job from two searches dedupes.
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/3901/"
    assert jobs[1].title == "Data Analyst Intern"


def test_linkedin_detects_easy_apply(linkedin, page):
    page.goto(fixture_url("linkedin_detail_easyapply.html"))
    assert linkedin.is_easy_apply() is True


def test_linkedin_easy_apply_posting_routes_to_the_board(linkedin, page):
    job = Job(
        site="linkedin", title="", company="",
        url=fixture_url("linkedin_detail_easyapply.html"),
    )
    job = linkedin.get_job_details(job)

    assert job.title == "Frontend Engineer Intern"
    assert job.company == "Fixture Labs"
    assert "React" in job.description
    assert job.posted == "3 days ago"
    assert job.external_url == ""
    assert job.apply_target == ApplyTarget.BOARD


def test_linkedin_external_posting_routes_to_the_career_page(linkedin, page):
    """The spec's core routing rule: follow the link off LinkedIn."""
    job = Job(
        site="linkedin", title="", company="",
        url=fixture_url("linkedin_detail_external.html"),
    )
    job = linkedin.get_job_details(job)

    assert linkedin.is_easy_apply() is False
    assert job.apply_target == ApplyTarget.CAREER_PAGE
    assert job.external_url.endswith("lever.html")
    assert job.apply_url() == job.external_url


def test_linkedin_easy_apply_fills_the_modal(linkedin, page, cfg, gemini):
    page.goto(fixture_url("linkedin_detail_easyapply.html"))
    job = Job(
        site="linkedin",
        title="Frontend Engineer Intern",
        company="Fixture Labs",
        url=page.url,
        description="React and JavaScript internship.",
        apply_target=ApplyTarget.BOARD,
    )
    ctx = make_ctx(cfg, gemini, job)
    outcome = linkedin.apply(job, ctx)

    assert page.input_value("#ea-email") == cfg.profile.email
    assert page.input_value("#ea-phone") == cfg.profile.phone
    # React experience must not be inflated past the profile.
    assert page.input_value("#ea-years") == str(cfg.profile.years_of_experience)
    onsite = page.evaluate(
        "() => [...document.querySelectorAll(\"input[name=onsite]\")]"
        ".filter(i => i.checked).map(i => i.value)"
    )
    assert onsite == ["Yes"]
    assert outcome.status in ("filled", "needs_review")


def test_linkedin_unticks_follow_company(linkedin, page, cfg, gemini):
    page.goto(fixture_url("linkedin_detail_easyapply.html"))
    assert page.is_checked("#follow-company-checkbox") is True

    job = Job(site="linkedin", title="X", company="Y", url=page.url, description="React")
    linkedin.apply(job, make_ctx(cfg, gemini, job))

    assert page.is_checked("#follow-company-checkbox") is False, (
        "following the company should not be a silent side effect"
    )


def test_linkedin_apply_refuses_a_non_easy_apply_posting(linkedin, page, cfg, gemini):
    page.goto(fixture_url("linkedin_detail_external.html"))
    job = Job(site="linkedin", title="X", company="Y", url=page.url)
    outcome = linkedin.apply(job, make_ctx(cfg, gemini, job))
    assert outcome.status == "needs_review"
    assert "not an Easy Apply" in outcome.reason


# ================================================================ Naukri


@pytest.fixture
def naukri(cfg, page, gemini, db, pacer):
    return make_source(Naukri, cfg, page, gemini, db, pacer)


def test_naukri_search_url_uses_seo_path(naukri):
    url = naukri.search_url("Software Developer Fresher", "Noida")
    assert "software-developer-fresher-jobs-in-noida" in url
    assert "experience=0" in url


def test_naukri_reads_search_cards(naukri, page):
    page.goto(fixture_url("naukri_search.html"))
    jobs = naukri._cards()

    assert len(jobs) == 2
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].company == "Fixture Labs"
    assert jobs[0].location == "Noida"
    assert jobs[0].extra["experience"] == "0-1 Yrs"
    assert "?" not in jobs[0].url, "tracking query should be stripped"


def test_naukri_routes_to_the_company_site(naukri, page):
    job = Job(
        site="naukri", title="", company="",
        url=fixture_url("naukri_detail_external.html"),
    )
    job = naukri.get_job_details(job)

    assert job.title == "Software Engineer"
    assert job.company == "Fixture Labs"
    assert "React" in job.description
    assert job.apply_target == ApplyTarget.CAREER_PAGE
    assert job.external_url.endswith("greenhouse.html")


def test_naukri_in_site_apply_is_left_for_the_user(naukri, page, cfg, gemini):
    """Naukri submits on the first click, so the bot must not press it."""
    page.goto(fixture_url("naukri_detail_external.html"))
    job = Job(site="naukri", title="X", company="Y", url=page.url, description="React")
    ctx = make_ctx(cfg, gemini, job)
    outcome = naukri.apply(job, ctx)

    assert outcome.status == "needs_review"
    assert ctx.report.needs_review is True


# ================================================================ Indeed


@pytest.fixture
def indeed(cfg, page, gemini, db, pacer):
    return make_source(Indeed, cfg, page, gemini, db, pacer)


def test_indeed_search_url(indeed):
    url = indeed.search_url("software engineer intern", "Noida, Uttar Pradesh")
    assert "q=software+engineer+intern" in url
    assert "l=Noida%2C+Uttar+Pradesh" in url
    assert "fromage=7" in url


def test_indeed_reads_search_cards(indeed, page):
    page.goto(fixture_url("indeed_search.html"))
    jobs = indeed._cards()

    assert len(jobs) == 2
    assert jobs[0].title == "Software Engineer Intern"
    assert jobs[0].company == "Fixture Labs"
    assert jobs[0].location == "Noida, Uttar Pradesh"
    # The job key becomes a stable URL, not a tracking redirect.
    assert jobs[0].url == "https://in.indeed.com/viewjob?jk=ind8001"
    assert "25,000" in jobs[0].stipend


def test_indeed_routes_to_the_company_site(indeed, page):
    job = Job(
        site="indeed", title="", company="",
        url=fixture_url("indeed_detail_external.html"),
    )
    job = indeed.get_job_details(job)

    assert job.title == "Software Engineer Intern"
    assert job.company == "Fixture Labs"
    assert "React" in job.description
    assert job.apply_target == ApplyTarget.CAREER_PAGE
    assert job.external_url.endswith("ashby.html")


def test_indeed_apply_refuses_a_non_indeed_apply_posting(indeed, page, cfg, gemini):
    page.goto(fixture_url("indeed_detail_external.html"))
    job = Job(site="indeed", title="X", company="Y", url=page.url)
    outcome = indeed.apply(job, make_ctx(cfg, gemini, job))
    assert outcome.status == "needs_review"


# ======================================================= shared guarantees


@pytest.mark.parametrize("cls", [LinkedIn, Naukri, Indeed])
def test_boards_are_excluded_from_auto_mode(cls):
    from runner import NEVER_AUTO

    assert cls.name in NEVER_AUTO


@pytest.mark.parametrize("cls", [LinkedIn, Naukri, Indeed])
def test_a_captcha_stops_the_site(cls, cfg, page, gemini, db, pacer):
    from models import SiteBlocked

    source = make_source(cls, cfg, page, gemini, db, pacer)
    page.set_content(
        "<html><body><h1>Let's do a quick security check</h1>"
        "<p>Please verify you are human to continue.</p></body></html>"
    )
    with pytest.raises(SiteBlocked) as excinfo:
        source.guard()
    assert excinfo.value.site == cls.name
    assert excinfo.value.screenshot, "a block must be screenshotted"
