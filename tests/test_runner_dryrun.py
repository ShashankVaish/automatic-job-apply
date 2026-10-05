"""Stage 2/3: the whole workflow end to end in --dry-run, against fixtures.

Proves the chain works: search -> dedupe -> score -> route -> fill -> log, with
nothing ever submitted.
"""
from __future__ import annotations

from typing import Iterator

import pytest

from limits import RunBudget
from models import ApplyTarget, Job
from runner import Runner
from sites.internshala import Internshala
from tests.conftest import fixture_url


class FakeSession:
    """Stands in for BrowserSession, handing out pages from a test context."""

    def __init__(self, context) -> None:
        self.context = context
        self.pages = []

    def new_page(self):
        p = self.context.new_page()
        p.set_default_timeout(5000)
        self.pages.append(p)
        return p

    def close(self) -> None:
        for p in self.pages:
            try:
                p.close()
            except Exception:
                pass


class FixtureInternshala(Internshala):
    """Internshala pointed at the local fixture pages."""

    def search(self) -> Iterator[Job]:
        self.goto(fixture_url("internshala_search.html"))
        for job in self._cards():
            yield job


@pytest.fixture
def session(offline_context):
    context, attempted = offline_context
    s = FakeSession(context)
    yield s
    s.close()
    assert not attempted, "the runner tried to reach the network: " + ", ".join(attempted[:5])


@pytest.fixture
def runner(cfg, db, gemini, session, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "load_source", lambda name: FixtureInternshala)
    return Runner(
        cfg,
        db,
        gemini,
        session,
        mode="dry-run",
        budget=RunBudget(cfg, db),
        skip_login=True,
    )


# --------------------------------------------------------------- happy path


def test_dry_run_processes_every_card(runner, db):
    stats = runner.run(["internshala"])
    site = stats.per_site["internshala"]

    assert site.found == 3
    # The unpaid 3+ years posting must be skipped on score.
    assert site.skipped >= 1
    assert db.all_rows(), "every job should be logged"


def test_dry_run_never_logs_anything_as_applied(runner, db):
    runner.run(["internshala"])
    statuses = {row["status"] for row in db.all_rows()}
    assert "applied" not in statuses, "a dry run must never record an application"


def test_dry_run_records_routing_and_scores(runner, db):
    runner.run(["internshala"])
    by_role = {row["role"]: row for row in db.all_rows()}

    external = by_role["Software Engineer Intern"]
    assert external["apply_target"] == ApplyTarget.CAREER_PAGE.value
    assert external["ats"] == "greenhouse"
    assert external["match_score"] is not None
    assert external["resume_variant"] in {"frontend", "data", "general"}

    in_board = by_role["Frontend Development Internship"]
    assert in_board["apply_target"] == ApplyTarget.BOARD.value


def test_low_scoring_job_is_skipped_with_a_reason(runner, db):
    runner.run(["internshala"])
    row = {r["role"]: r for r in db.all_rows()}["Content Writing Internship"]

    assert row["status"] == "skipped"
    assert "threshold" in (row["error"] or "")
    # The reason has to name why, so the log is useful later.
    assert "unpaid" in (row["error"] or "").lower() or "years" in (row["error"] or "")


def test_email_posting_is_routed_to_outreach(cfg, db, gemini, session, monkeypatch):
    """A posting that says "email us your CV" goes to the outreach queue.

    The address in the job post is the first HR source outreach looks for, so
    the runner records the job and leaves composing to --email-queue. Nothing
    is sent, and no separate draft file is written.
    """
    import runner as runner_module

    monkeypatch.setattr(runner_module, "load_source", lambda name: FixtureInternshala)
    # Pass the score threshold so the email route is actually reached.
    gemini.score = 90
    gemini.fail_on = set()
    r = Runner(
        cfg, db, gemini, session, mode="dry-run",
        budget=RunBudget(cfg, db), skip_login=True,
    )

    page = session.new_page()
    source = FixtureInternshala(cfg, page, gemini=gemini, db=db, pacer=r.pacer)
    job = Job(
        site="internshala",
        title="Content Writing Internship",
        company="Fixture Media",
        url=fixture_url("internshala_detail_email.html"),
    )
    job = source.get_job_details(job)
    assert job.apply_target == ApplyTarget.EMAIL

    assert job.apply_email == "careers@fixturemedia.example"

    outcome = r.apply_by_email(job, cfg.resume("general"))
    assert outcome.status == "drafted"
    assert "--email-queue" in outcome.reason

    # The job and its address are recorded so outreach can pick them up.
    r.record(job, "drafted", match_score=90, resume_variant="general")
    row = [x for x in db.all_rows() if x["company"] == "Fixture Media"][0]
    assert row["apply_email"] == "careers@fixturemedia.example"
    assert row["description"]

    # And the address really is discoverable from the stored description.
    from contacts import contacts_from_job_post

    found = contacts_from_job_post(row["company"], row["description"], row["url"])
    assert [c.email for c in found] == ["careers@fixturemedia.example"]


# ------------------------------------------------------------------ dedupe


def test_second_run_skips_everything_as_duplicates(runner, db):
    runner.run(["internshala"])
    first = len(db.all_rows())

    runner.run(["internshala"])
    assert len(db.all_rows()) == first, "re-running must not create new rows"

    # Everything is now either skipped as a duplicate or was already skipped.
    reasons = [r["error"] or "" for r in db.all_rows()]
    assert any("already handled this URL" in r or "duplicate" in r for r in reasons)


# ------------------------------------------------------------------- limits


def test_run_cap_stops_the_run(cfg, db, gemini, session, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "load_source", lambda name: FixtureInternshala)
    r = Runner(
        cfg, db, gemini, session, mode="dry-run",
        budget=RunBudget(cfg, db, run_max=1), skip_login=True,
    )
    r.run(["internshala"])
    assert r.budget.used_this_run <= 1


def test_daily_site_cap_is_respected(cfg, db, gemini, session, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "load_source", lambda name: FixtureInternshala)
    cfg.limits.daily["internshala"] = 0
    budget = RunBudget(cfg, db)
    usable, rejected = budget.usable_sites(["internshala"])
    assert usable == []
    assert "daily limit reached" in rejected["internshala"]


def test_blocked_site_is_not_run_again_today(cfg, db):
    db.block_site("internshala", "page says: verify you are human")
    budget = RunBudget(cfg, db)
    usable, rejected = budget.usable_sites(["internshala"])
    assert usable == []
    assert "blocked earlier today" in rejected["internshala"]


# -------------------------------------------------------- failure isolation


def test_a_failing_job_does_not_stop_the_run(cfg, db, gemini, session, monkeypatch):
    """One job blowing up must not end the whole site."""
    import runner as runner_module

    monkeypatch.setattr(runner_module, "load_source", lambda name: FixtureInternshala)
    r = Runner(
        cfg, db, gemini, session, mode="dry-run",
        budget=RunBudget(cfg, db), skip_login=True,
    )

    calls = {"n": 0}
    original = r.handle_job

    def flaky(source, job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return original(source, job)

    monkeypatch.setattr(r, "handle_job", flaky)
    stats = r.run(["internshala"])

    assert calls["n"] == 3, "all three jobs should still be attempted"
    assert stats.per_site["internshala"].failed == 1
    failed = [row for row in db.all_rows() if row["status"] == "failed"]
    assert failed and "boom" in (failed[0]["error"] or "")


def test_match_scoring_failure_becomes_needs_review(cfg, db, gemini, session, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "load_source", lambda name: FixtureInternshala)
    gemini.fail_on = {"score_match"}
    r = Runner(
        cfg, db, gemini, session, mode="dry-run",
        budget=RunBudget(cfg, db), skip_login=True,
    )
    stats = r.run(["internshala"])

    assert stats.per_site["internshala"].needs_review == 3
    rows = db.all_rows()
    assert all("match scoring failed" in (row["error"] or "") for row in rows)
