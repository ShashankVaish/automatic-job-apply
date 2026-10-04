"""Stage 5: email application drafts, Gmail drafts, follow-ups and the report.

The hard guarantee tested here: nothing is ever sent. Gmail is exercised only
through a mock service that records the API calls, so no consent screen opens
and no message leaves the machine.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta

import pytest

from email_drafts import FOLLOWUP_COLUMNS, build_followups, draft_application_email, safe_name
from models import ApplyTarget, Job


def email_job() -> Job:
    return Job(
        site="internshala",
        title="Content Writing Intern",
        company="Fixture Media / Delhi",
        url="https://internshala.com/internship/detail/9001",
        description="Send your resume to careers@fixturemedia.example to apply.",
        apply_target=ApplyTarget.EMAIL,
        apply_email="careers@fixturemedia.example",
    )


# ----------------------------------------------------------- draft files


def test_draft_is_written_to_disk(cfg, gemini):
    path = draft_application_email(cfg, gemini, email_job(), cfg.resume("general"))

    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "careers@fixturemedia.example" in text
    assert "Content Writing Intern" in text
    assert cfg.resume("general").public_link in text
    assert str(cfg.resume("general").abs_pdf_path) in text


def test_draft_says_it_was_not_sent(cfg, gemini):
    path = draft_application_email(cfg, gemini, email_job(), cfg.resume("general"))
    assert "NOT SENT" in path.read_text(encoding="utf-8")


def test_draft_filename_is_filesystem_safe(cfg, gemini):
    """The company name has a slash in it, which must not create a directory."""
    path = draft_application_email(cfg, gemini, email_job(), cfg.resume("general"))
    assert path.parent == cfg.abs_path(cfg.email.drafts_dir)
    assert "/" not in path.name and "\\" not in path.name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Fixture Media / Delhi", "Fixture-Media-Delhi"),
        ("  spaces  ", "spaces"),
        ("!!!", "unknown"),
        ("", "unknown"),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


def test_gmail_is_not_touched_when_disabled(cfg, gemini, monkeypatch):
    calls = []
    import gmail_client

    monkeypatch.setattr(gmail_client, "get_service", lambda c: calls.append("called"))
    assert cfg.email.gmail_api is False
    draft_application_email(cfg, gemini, email_job(), cfg.resume("general"))
    assert calls == [], "Gmail must not be contacted unless gmail_api is true"


# ------------------------------------------------------------ Gmail mock


class MockDrafts:
    def __init__(self, log: list) -> None:
        self.log = log

    def create(self, userId: str, body: dict):
        self.log.append(("drafts.create", userId, body))

        class Exec:
            @staticmethod
            def execute():
                return {"id": "draft-123"}

        return Exec()


class MockUsers:
    def __init__(self, log: list) -> None:
        self.log = log

    def drafts(self) -> MockDrafts:
        return MockDrafts(self.log)

    def messages(self):
        raise AssertionError("nothing in this project may call messages()")


class MockService:
    def __init__(self) -> None:
        self.log: list = []

    def users(self) -> MockUsers:
        return MockUsers(self.log)


def test_create_draft_uses_the_drafts_api_only(cfg):
    from gmail_client import create_draft

    service = MockService()
    draft_id = create_draft(
        cfg,
        to="careers@fixturemedia.example",
        subject="Application for Content Writing Intern",
        body="Hello,\n\nPlease find my resume attached.\n",
        attachment=cfg.resume("general").abs_pdf_path,
        service=service,
    )

    assert draft_id == "draft-123"
    assert len(service.log) == 1
    method, user, body = service.log[0]
    assert method == "drafts.create"
    assert user == "me"
    assert "raw" in body["message"]


def test_draft_message_carries_the_attachment(cfg):
    import base64

    from gmail_client import build_message, encode

    message = build_message(
        to="x@example.com",
        subject="Application",
        body="Body text here",
        sender=cfg.profile.email,
        attachment=cfg.resume("frontend").abs_pdf_path,
    )
    raw = base64.urlsafe_b64decode(encode(message)).decode("utf-8", "replace")

    assert "x@example.com" in raw
    assert "Body text here" in raw
    assert "frontend.pdf" in raw


def test_build_message_survives_a_missing_attachment(cfg):
    from gmail_client import build_message

    message = build_message(
        to="x@example.com", subject="s", body="b", attachment="./nope/missing.pdf"
    )
    assert message["To"] == "x@example.com"


def test_gmail_raises_a_clear_error_without_credentials(cfg, monkeypatch, tmp_path):
    from gmail_client import GmailNotConfigured, get_service

    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(tmp_path / "nope-token.json"))
    with pytest.raises(GmailNotConfigured) as excinfo:
        get_service(cfg)
    assert "credentials" in str(excinfo.value).lower() or "missing" in str(excinfo.value)


# ------------------------------------------------------------ follow-ups


def applied_row(db, days_ago: int, company: str = "Fixture Labs", site: str = "linkedin"):
    """Insert an application and backdate it."""
    db.record(
        site=site,
        company=company,
        role="Frontend Engineer Intern",
        url="https://example.com/jobs/" + company.replace(" ", "") + str(days_ago),
        status="applied",
        apply_target="career_page",
        match_score=88,
        resume_variant="frontend",
    )
    old = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    with db.conn:
        db.conn.execute(
            "UPDATE applications SET date=? WHERE company=? AND date=?",
            (old, company, date.today().strftime("%Y-%m-%d")),
        )


def test_nothing_is_due_before_the_window(cfg, db, gemini):
    applied_row(db, days_ago=2)
    assert build_followups(cfg, db, gemini) == []
    assert not cfg.abs_path(cfg.followups.csv_path).exists()


def test_followup_is_queued_after_six_days(cfg, db, gemini):
    applied_row(db, days_ago=6)
    rows = build_followups(cfg, db, gemini)

    assert len(rows) == 1
    assert rows[0]["company"] == "Fixture Labs"
    assert rows[0]["sent"] == "no"
    assert len(rows[0]["message"].strip().split("\n")) == 3, "spec asks for 3 lines"


def test_followup_csv_has_the_expected_columns(cfg, db, gemini):
    applied_row(db, days_ago=7)
    build_followups(cfg, db, gemini)

    path = cfg.abs_path(cfg.followups.csv_path)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == FOLLOWUP_COLUMNS
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["channel"] == "linkedin"


def test_channel_is_email_for_non_linkedin_applications(cfg, db, gemini):
    applied_row(db, days_ago=7, company="Fixture Web", site="internshala")
    rows = build_followups(cfg, db, gemini)
    assert rows[0]["channel"] == "email"


def test_a_followup_is_only_queued_once(cfg, db, gemini):
    applied_row(db, days_ago=8)
    assert len(build_followups(cfg, db, gemini)) == 1
    assert build_followups(cfg, db, gemini) == [], "must not queue the same job twice"


def test_skipped_and_failed_jobs_never_get_followups(cfg, db, gemini):
    db.record(
        site="linkedin", company="Fixture Skip", role="X",
        url="https://example.com/skip", status="skipped", error="score too low",
    )
    db.record(
        site="linkedin", company="Fixture Fail", role="Y",
        url="https://example.com/fail", status="failed", error="timeout",
    )
    with db.conn:
        db.conn.execute(
            "UPDATE applications SET date=?",
            ((date.today() - timedelta(days=30)).strftime("%Y-%m-%d"),),
        )
    assert build_followups(cfg, db, gemini) == []


def test_a_gemini_failure_skips_that_followup_only(cfg, db, gemini):
    applied_row(db, days_ago=7, company="Fixture One")
    applied_row(db, days_ago=7, company="Fixture Two")
    gemini.fail_on = {"follow_up"}
    assert build_followups(cfg, db, gemini) == []
    # Neither was marked done, so they'll be retried next run.
    assert len(db.due_followups(cfg.followups.days_after)) == 2


# --------------------------------------------------------------- report


def test_report_runs_on_an_empty_database(cfg, db, capsys):
    from main import cmd_report

    assert cmd_report(cfg, db, 7) == 0
    assert "No activity" in capsys.readouterr().out


def test_report_shows_full_dates_and_no_broken_characters(cfg, db, capsys):
    """Regression: the date column used to be clipped to "2026-10-0?"."""
    from main import cmd_report

    db.record(
        site="internshala",
        company="A Company With A Very Long Name Indeed Ltd",
        role="Senior Staff Frontend Engineering Intern Trainee",
        url="https://example.com/1",
        status="applied",
        apply_target="career_page",
        ats="greenhouse",
        match_score=91,
        resume_variant="frontend",
    )
    assert cmd_report(cfg, db, 7) == 0

    out = capsys.readouterr().out
    today = date.today().strftime("%Y-%m-%d")
    assert today in out, "the full date must be visible, not truncated"
    assert "�" not in out, "no replacement characters"
    assert "…" not in out, "no unicode ellipsis - it breaks cp1252 terminals"
    assert "..." in out, "long values are clipped with ASCII dots instead"


def test_report_lists_skip_reasons(cfg, db, capsys):
    from main import cmd_report

    db.record(
        site="internshala", company="Fixture Media", role="Unpaid Intern",
        url="https://example.com/2", status="skipped",
        error="score 25 < threshold 70: unpaid | red flags: unpaid",
        match_score=25,
    )
    cmd_report(cfg, db, 7)
    out = capsys.readouterr().out
    assert "Why jobs were skipped" in out
    assert "unpaid" in out


def test_export_writes_every_column(cfg, db):
    from db import CSV_COLUMNS

    db.record(
        site="linkedin", company="Fixture", role="Role",
        url="https://example.com/3", status="applied", apply_target="board",
        match_score=80, resume_variant="general",
    )
    out = db.export_csv(cfg.abs_path(cfg.paths.applications_csv))
    with out.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_COLUMNS
        assert len(list(reader)) == 1
