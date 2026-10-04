"""Stage 6: cold email outreach.

The guarantees under test, in order of importance:
  1. nothing is ever sent - every message is a file in ./outbox/
  2. email addresses are never guessed, only read from your CSV or from a page
     the company published
  3. the same company is never cold-emailed twice inside the dedupe window
  4. the follow-up ladder stops at max_followups
"""
from __future__ import annotations

import csv

import pytest

from outreach import (
    LEDGER_COLUMNS,
    Target,
    best_address,
    classify_address,
    draft_outreach,
    draft_outreach_followups,
    enrich_from_page,
    load_csv_targets,
    load_targets,
    load_url_targets,
)
from tests.conftest import fixture_url


# ----------------------------------------------------------- addresses


@pytest.mark.parametrize(
    "email",
    [
        "noreply@fixture.example",
        "no-reply@fixture.example",
        "postmaster@fixture.example",
        "support@fixture.example",
        "sales@fixture.example",
        "legal@fixture.example",
        "unsubscribe@fixture.example",
    ],
)
def test_junk_addresses_are_never_used(email):
    assert classify_address(email) == -1
    assert best_address([email]) == ""


def test_a_hiring_address_wins_over_a_generic_one():
    assert (
        best_address(["info@fixture.example", "careers@fixture.example"])
        == "careers@fixture.example"
    )
    assert (
        best_address(["hello@fixture.example", "jobs@fixture.example"])
        == "jobs@fixture.example"
    )


def test_addresses_are_normalised_and_deduped():
    assert best_address(["  Careers@Fixture.Example,", "careers@fixture.example"]) == (
        "careers@fixture.example"
    )


def test_no_usable_address_returns_empty():
    assert best_address([]) == ""
    assert best_address(["noreply@x.example", "abuse@x.example"]) == ""


def test_a_personal_address_is_accepted():
    assert best_address(["priya.nair@fixture.example"]) == "priya.nair@fixture.example"


# -------------------------------------------------------------- inputs


def test_loads_the_example_csv(cfg):
    targets = load_csv_targets(cfg.abs_path("./inputs/companies.example.csv"))

    assert len(targets) == 3
    assert targets[0].company == "Fixture Labs"
    assert targets[0].email == "careers@fixturelabs.example"
    assert targets[1].contact_name == "Priya Nair"
    # The third row deliberately has no address.
    assert targets[2].email == ""


def test_loads_the_example_url_list(cfg):
    targets = load_url_targets(cfg.abs_path("./inputs/job_urls.example.txt"))
    # Every line in the example is a comment, so nothing is loaded.
    assert targets == []


def test_url_list_skips_comments_and_blanks(cfg, tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text(
        "# a comment\n\nhttps://fixture.example/careers\n   \n"
        "https://other.example/jobs\n",
        encoding="utf-8",
    )
    targets = load_url_targets(path)
    assert [t.source_url for t in targets] == [
        "https://fixture.example/careers",
        "https://other.example/jobs",
    ]
    assert all(t.source == "job_url" for t in targets)


def test_missing_input_files_are_not_an_error(cfg, tmp_path):
    assert load_csv_targets(tmp_path / "nope.csv") == []
    assert load_url_targets(tmp_path / "nope.txt") == []
    assert load_targets(cfg, csv_path="./nope.csv", urls_path="./nope.txt") == []


# ------------------------------------------------------- page enrichment


def test_reads_company_role_and_address_from_a_page(cfg, page):
    target = Target(source="job_url", source_url=fixture_url("careers_with_email.html"))
    enriched = enrich_from_page(page, target)

    assert "Fixture Robotics" in enriched.company
    assert enriched.email == "careers@fixturerobotics.example"
    assert enriched.role
    assert "robotics" in enriched.context.lower()


def test_a_page_with_only_a_noreply_address_yields_nothing(cfg, page):
    target = Target(source="job_url", source_url=fixture_url("careers_no_email.html"))
    enriched = enrich_from_page(page, target, follow_contact_page=False)
    assert enriched.email == "", "noreply must never become an outreach address"


def test_an_unreachable_page_does_not_raise(cfg, page):
    target = Target(source="job_url", source_url="file:///definitely/not/here.html")
    assert enrich_from_page(page, target).email == ""


# ---------------------------------------------------------- drafting


def outbox_files(cfg):
    return sorted(p.name for p in cfg.abs_path(cfg.outreach.outbox_dir).glob("*.txt"))


def test_drafts_are_written_and_nothing_is_sent(cfg, db, gemini):
    targets = [
        Target(company="Fixture Labs", email="careers@fixturelabs.example",
               role="Frontend Developer Intern", source="csv"),
    ]
    stats = draft_outreach(cfg, db, gemini, targets)

    assert stats.drafted == 1
    files = outbox_files(cfg)
    assert files == ["Fixture-Labs.txt"]

    text = (cfg.abs_path(cfg.outreach.outbox_dir) / files[0]).read_text(encoding="utf-8")
    assert "careers@fixturelabs.example" in text
    assert "NOT SENT" in text
    assert "COLD OUTREACH" in text


def test_draft_includes_the_resume_link_and_attachment_path(cfg, db, gemini):
    targets = [Target(company="Fixture Labs", email="careers@fixturelabs.example",
                      role="Frontend Developer Intern", source="csv")]
    draft_outreach(cfg, db, gemini, targets)
    text = (cfg.abs_path(cfg.outreach.outbox_dir) / "Fixture-Labs.txt").read_text(encoding="utf-8")

    row = db.outreach_seen("careers@fixturelabs.example")
    resume = cfg.resume(row["resume_variant"])
    assert resume.public_link in text
    assert str(resume.abs_pdf_path) in text


def test_gmail_is_not_touched_when_disabled(cfg, db, gemini, monkeypatch):
    import gmail_client

    calls = []
    monkeypatch.setattr(gmail_client, "get_service", lambda c: calls.append(1))
    draft_outreach(
        cfg, db, gemini,
        [Target(company="Fixture Labs", email="careers@fixturelabs.example", source="csv")],
    )
    assert calls == []


def test_a_target_with_no_address_is_flagged_not_guessed(cfg, db, gemini):
    """The critical anti-spam rule: we do not invent firstname@company.com."""
    targets = [Target(company="Fixture Startup", role="SDE Intern", source="csv")]
    stats = draft_outreach(cfg, db, gemini, targets)

    assert stats.drafted == 0
    assert stats.needs_email == 1
    assert outbox_files(cfg) == [], "no draft without a real address"

    rows = db.outreach_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "needs_email"
    assert "never guessed" in rows[0]["note"]


def test_the_ledger_records_every_draft(cfg, db, gemini):
    draft_outreach(
        cfg, db, gemini,
        [Target(company="Fixture Labs", email="careers@fixturelabs.example",
                role="Frontend Developer Intern", source="csv")],
    )
    ledger = cfg.abs_path(cfg.outreach.outbox_dir) / "outreach.csv"
    with ledger.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == LEDGER_COLUMNS
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["sent"] == "no"
    assert rows[0]["status"] == "drafted"


def test_resume_variant_is_chosen_per_company(cfg, db, gemini):
    draft_outreach(
        cfg, db, gemini,
        [
            Target(company="Fixture Analytics", email="jobs@fixtureanalytics.example",
                   role="Data Analyst Intern", notes="SQL and pandas work", source="csv"),
        ],
    )
    row = db.outreach_seen("jobs@fixtureanalytics.example")
    assert row["resume_variant"] == "data"


# -------------------------------------------------------------- dedupe


def test_the_same_address_is_never_drafted_twice(cfg, db, gemini):
    target = Target(company="Fixture Labs", email="careers@fixturelabs.example", source="csv")
    assert draft_outreach(cfg, db, gemini, [target]).drafted == 1

    again = draft_outreach(cfg, db, gemini, [target])
    assert again.drafted == 0
    assert again.skipped == 1
    assert any("already drafted" in n for n in again.notes)


def test_the_same_company_at_a_new_address_is_still_skipped(cfg, db, gemini):
    first = Target(company="Fixture Labs", email="careers@fixturelabs.example", source="csv")
    second = Target(company="fixture  labs", email="jobs@fixturelabs.example", source="csv")
    draft_outreach(cfg, db, gemini, [first])

    stats = draft_outreach(cfg, db, gemini, [second], dedupe_days=90)
    assert stats.drafted == 0
    assert any("already contacted" in n for n in stats.notes)


def test_dedupe_window_can_be_shortened(cfg, db, gemini):
    draft_outreach(
        cfg, db, gemini,
        [Target(company="Fixture Labs", email="a@fixturelabs.example", source="csv")],
    )
    stats = draft_outreach(
        cfg, db, gemini,
        [Target(company="Fixture Labs", email="b@fixturelabs.example", source="csv")],
        dedupe_days=0,
    )
    assert stats.drafted == 1


# -------------------------------------------------------------- limits


def test_limit_caps_the_run(cfg, db, gemini):
    targets = [
        Target(company="Company " + str(i), email="careers@c{0}.example".format(i),
               source="csv")
        for i in range(5)
    ]
    stats = draft_outreach(cfg, db, gemini, targets, limit=2)
    assert stats.drafted == 2
    assert any("--max" in n for n in stats.notes)


def test_daily_cap_is_respected(cfg, db, gemini):
    cfg.limits.daily["outreach"] = 1
    targets = [
        Target(company="Company " + str(i), email="careers@c{0}.example".format(i),
               source="csv")
        for i in range(3)
    ]
    stats = draft_outreach(cfg, db, gemini, targets)
    assert stats.drafted == 1
    assert any("daily limit" in n for n in stats.notes)


def test_one_failure_does_not_stop_the_batch(cfg, db, gemini, monkeypatch):
    calls = {"n": 0}
    original = gemini.outreach_email

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model blew up")
        return original(**kwargs)

    monkeypatch.setattr(gemini, "outreach_email", flaky)
    targets = [
        Target(company="First Co", email="careers@first.example", source="csv"),
        Target(company="Second Co", email="careers@second.example", source="csv"),
    ]
    stats = draft_outreach(cfg, db, gemini, targets)
    assert stats.failed == 1
    assert stats.drafted == 1


# ---------------------------------------------------------- follow-ups


def backdate(db, email: str, days: int) -> None:
    from datetime import date, timedelta

    old = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.conn:
        db.conn.execute(
            "UPDATE outreach SET date=?, last_contact=? WHERE email=?",
            (old, old, email),
        )


def seed_outreach(cfg, db, gemini, days_ago: int = 7):
    draft_outreach(
        cfg, db, gemini,
        [Target(company="Fixture Labs", email="careers@fixturelabs.example",
                role="Frontend Developer Intern", source="csv")],
    )
    backdate(db, "careers@fixturelabs.example", days_ago)


def test_nothing_is_due_before_the_window(cfg, db, gemini):
    seed_outreach(cfg, db, gemini, days_ago=1)
    assert draft_outreach_followups(cfg, db, gemini) == []


def test_first_followup_is_drafted(cfg, db, gemini):
    seed_outreach(cfg, db, gemini, days_ago=7)
    rows = draft_outreach_followups(cfg, db, gemini)

    assert len(rows) == 1
    assert rows[0]["stage"] == "1"
    assert rows[0]["sent"] == "no"
    assert "Fixture-Labs_followup1.txt" in outbox_files(cfg)

    text = (cfg.abs_path(cfg.outreach.outbox_dir) / "Fixture-Labs_followup1.txt").read_text(
        encoding="utf-8"
    )
    assert "FOLLOW-UP 1" in text
    assert "NOT SENT" in text


def test_the_ladder_stops_at_max_followups(cfg, db, gemini):
    seed_outreach(cfg, db, gemini, days_ago=7)

    assert len(draft_outreach_followups(cfg, db, gemini, max_stage=2)) == 1
    # Still inside the window for the next stage until last_contact ages.
    assert draft_outreach_followups(cfg, db, gemini, max_stage=2) == []

    backdate(db, "careers@fixturelabs.example", 20)
    with db.conn:
        db.conn.execute(
            "UPDATE outreach SET followup_stage=1 WHERE email=?",
            ("careers@fixturelabs.example",),
        )
    second = draft_outreach_followups(cfg, db, gemini, max_stage=2)
    assert len(second) == 1
    assert second[0]["stage"] == "2"
    assert "last time" in second[0]["message"].lower()

    # Stage 2 is the end of the ladder.
    backdate(db, "careers@fixturelabs.example", 40)
    with db.conn:
        db.conn.execute(
            "UPDATE outreach SET followup_stage=2 WHERE email=?",
            ("careers@fixturelabs.example",),
        )
    assert draft_outreach_followups(cfg, db, gemini, max_stage=2) == []


def test_needs_email_rows_never_get_followups(cfg, db, gemini):
    draft_outreach(cfg, db, gemini, [Target(company="No Address Co", source="csv")])
    with db.conn:
        db.conn.execute("UPDATE outreach SET date='2020-01-01', last_contact='2020-01-01'")
    assert draft_outreach_followups(cfg, db, gemini) == []


# ------------------------------------------------------------- report


def test_outreach_report_on_an_empty_database(cfg, db, capsys):
    from main import cmd_outreach_report

    assert cmd_outreach_report(cfg, db, 7) == 0
    assert "No outreach" in capsys.readouterr().out


def test_outreach_report_shows_drafts_and_says_nothing_was_sent(cfg, db, gemini, capsys):
    draft_outreach(
        cfg, db, gemini,
        [Target(company="Fixture Labs", email="careers@fixturelabs.example", source="csv")],
    )
    from main import cmd_outreach_report

    assert cmd_outreach_report(cfg, db, 7) == 0
    out = capsys.readouterr().out
    assert "Fixture Labs" in out
    assert "Nothing has been sent" in out
    assert "…" not in out and "�" not in out
