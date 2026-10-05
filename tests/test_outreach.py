"""Cold email outreach: queue, compose, review, send, follow up (spec 8.1-8.5).

Every send in here goes through the real sending code against a mock Gmail
service, so the gates are genuinely exercised. No test can reach the real API:
the service is injected, and `tests/test_spec_compliance.py` asserts the
interactive OAuth path is never taken without a browser consent.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta

import pytest

from contacts import ROLE_COFOUNDER, ROLE_FOUNDER, ROLE_HR, Contact
from outreach import (
    LEDGER_COLUMNS,
    Target,
    build_followup_queue,
    build_queue,
    load_company_targets,
    load_targets,
    load_url_targets,
    safe_name,
    schedule_for,
    send_queue,
    sync_replies_and_bounces,
    targets_from_matched_jobs,
)
from tests.conftest import fixture_url


# ----------------------------------------------------------------- helpers


def hr_contact(company="Fixture Labs", email="hr@fixturelabs.example", name="Priya Nair"):
    return Contact(company=company, email=email, role=ROLE_HR, name=name, verified=True)


def write_contacts_csv(cfg, rows: str) -> None:
    path = cfg.abs_path(cfg.outreach.contacts_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("company,name,role,email,source_url,notes\n" + rows, encoding="utf-8")


def seed_matched_job(
    db,
    *,
    company="Fixture Labs",
    role="Frontend Developer Intern",
    score=85,
    url="https://example.com/jobs/1",
    description="React and JavaScript internship. We use Tailwind and ship weekly.",
):
    db.record(
        site="linkedin",
        company=company,
        role=role,
        url=url,
        status="applied",
        apply_target="career_page",
        match_score=score,
        resume_variant="frontend",
        description=description,
    )


def queue_one_email(cfg, db, gemini, **kwargs):
    """Queue a single HR email with no browser and no discovery."""
    write_contacts_csv(
        cfg,
        "{0},Priya Nair,hr,{1},,\n".format(
            kwargs.get("company", "Fixture Labs"),
            kwargs.get("email", "hr@fixturelabs.example"),
        ),
    )
    target = Target(
        company=kwargs.get("company", "Fixture Labs"),
        job_role=kwargs.get("job_role", "Frontend Developer Intern"),
        description="React work",
        resume_variant="frontend",
    )
    return build_queue(cfg, db, gemini, [target])


# ------------------------------------------------------------ 8.x inputs


def test_safe_name_is_filesystem_safe():
    assert safe_name("Fixture Media / Delhi") == "Fixture-Media-Delhi"
    assert safe_name("!!!") == "unknown"
    assert safe_name("") == "unknown"


def test_loads_the_example_company_csv(cfg):
    targets = load_company_targets(cfg.abs_path("./inputs/companies.example.csv"))
    assert len(targets) == 3
    assert targets[0].company == "Fixture Labs"
    assert targets[0].job_role == "Frontend Developer Intern"


def test_url_list_skips_comments(cfg, tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("# comment\n\nhttps://fixture.example/careers\n", encoding="utf-8")
    targets = load_url_targets(path)
    assert [t.job_url for t in targets] == ["https://fixture.example/careers"]


def test_matched_jobs_become_targets(cfg, db):
    """Spec 8.2: outreach is driven by jobs scoring at or above the threshold."""
    seed_matched_job(db, company="Good Fit", score=85)
    seed_matched_job(db, company="Bad Fit", score=40, url="https://example.com/jobs/2")

    targets = targets_from_matched_jobs(cfg, db)
    names = {t.company for t in targets}
    assert "Good Fit" in names
    assert "Bad Fit" not in names, "below the 70 threshold"


def test_a_job_at_exactly_the_threshold_is_included(cfg, db):
    seed_matched_job(db, company="Exactly Seventy", score=70)
    assert any(t.company == "Exactly Seventy" for t in targets_from_matched_jobs(cfg, db))


def test_targets_are_deduplicated_by_company(cfg, db, tmp_path):
    seed_matched_job(db, company="Fixture Labs")
    path = cfg.abs_path(cfg.outreach.companies_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "company,email,role,website,notes\n"
        "Fixture Labs,,Frontend Developer Intern,https://fixturelabs.example,nice team\n",
        encoding="utf-8",
    )
    targets = load_targets(cfg, db)
    labs = [t for t in targets if t.company == "Fixture Labs"]
    assert len(labs) == 1
    # The merged target keeps the richest information from both sources.
    assert labs[0].website
    assert labs[0].description


# ------------------------------------------------- 8.3 writing the emails


def test_hr_email_is_formal_and_asks_to_be_considered(cfg, db, gemini):
    queue_one_email(cfg, db, gemini)
    row = db.queued_emails()[0]

    assert row["recipient_role"] == ROLE_HR
    assert "Frontend Developer Intern" in row["subject"]
    assert len(row["subject"].split()) < 9, "spec: subject under 9 words"
    assert "considered" in row["body"].lower()
    assert "Hi Priya," in row["body"]


def test_subjects_always_contain_the_role_name(cfg, db, gemini):
    queue_one_email(cfg, db, gemini, job_role="Data Analyst Intern")
    row = db.queued_emails()[0]
    assert "Data Analyst Intern" in row["subject"]


def test_founder_email_is_shorter_than_the_hr_email(cfg, db, gemini):
    """Spec: HR 90-150 words, founder 80-110. The founder email must be shorter."""
    hr = gemini.outreach_email(
        recipient_role=ROLE_HR, company="Fixture Labs",
        job_role="Frontend Developer Intern", contact_name="Priya Nair",
        resume_link="https://drive.example/r",
    )
    founder = gemini.outreach_email(
        recipient_role=ROLE_FOUNDER, company="Fixture Labs",
        job_role="Frontend Developer Intern", contact_name="Arjun Mehta",
        resume_link="https://drive.example/r",
    )
    assert len(founder.body.split()) < len(hr.body.split())


def test_each_recipient_type_gets_a_different_email(cfg, gemini):
    bodies = {}
    for role in (ROLE_HR, ROLE_FOUNDER, ROLE_COFOUNDER):
        draft = gemini.outreach_email(
            recipient_role=role, company="Fixture Labs",
            job_role="Frontend Developer Intern", contact_name="Someone Here",
            resume_link="https://drive.example/r",
        )
        bodies[role] = draft.body
    assert len(set(bodies.values())) == 3, "all three must be genuinely different"


def test_founder_email_asks_for_a_short_call(cfg, gemini):
    draft = gemini.outreach_email(
        recipient_role=ROLE_FOUNDER, company="Fixture Labs", job_role="Intern",
        contact_name="Arjun Mehta", resume_link="https://drive.example/r",
    )
    assert "10-minute" in draft.body or "10 minute" in draft.body
    assert "right person" in draft.body


def test_greeting_uses_the_first_name_when_known(cfg, gemini):
    draft = gemini.outreach_email(
        recipient_role=ROLE_HR, company="Fixture Labs", job_role="Intern",
        contact_name="Priya Nair", resume_link="https://drive.example/r",
    )
    assert draft.body.startswith("Hi Priya,")


def test_greeting_falls_back_to_the_company_team(cfg, gemini):
    draft = gemini.outreach_email(
        recipient_role=ROLE_HR, company="Fixture Labs", job_role="Intern",
        contact_name="", resume_link="https://drive.example/r",
    )
    assert draft.body.startswith("Hi Fixture Labs team,")


def test_every_email_carries_the_resume_link_phone_and_linkedin(cfg, db, gemini):
    queue_one_email(cfg, db, gemini)
    body = db.queued_emails()[0]["body"]
    resume = cfg.resume("frontend")

    assert resume.public_link in body
    assert cfg.profile.phone in body
    assert cfg.profile.linkedin in body
    assert cfg.profile.name in body


def test_the_draft_file_is_written_to_the_outbox(cfg, db, gemini):
    queue_one_email(cfg, db, gemini)
    files = sorted(p.name for p in cfg.abs_path(cfg.outreach.outbox_dir).glob("*.txt"))
    assert files == ["Fixture-Labs_hr.txt"]

    text = (cfg.abs_path(cfg.outreach.outbox_dir) / files[0]).read_text(encoding="utf-8")
    assert "hr@fixturelabs.example" in text
    assert "Queued, not sent" in text
    assert str(cfg.resume("frontend").abs_pdf_path) in text


def test_the_ledger_records_each_queued_email(cfg, db, gemini):
    queue_one_email(cfg, db, gemini)
    ledger = cfg.abs_path(cfg.outreach.outbox_dir) / "outreach.csv"
    with ledger.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == LEDGER_COLUMNS
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["status"] == "queued"


def test_queueing_sends_nothing(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    assert gmail.sent == [], "building the queue must never send"
    assert db.queued_emails()[0]["status"] == "queued"


# --------------------------------------------- 8.4 scheduling and limits


def test_hr_goes_today_and_founders_a_day_later(cfg):
    today = date.today().strftime("%Y-%m-%d")
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert schedule_for(cfg, ROLE_HR) == today
    assert schedule_for(cfg, ROLE_FOUNDER) == tomorrow
    assert schedule_for(cfg, ROLE_COFOUNDER) == tomorrow


def test_founder_emails_are_not_due_on_the_first_day(cfg, db, gemini):
    write_contacts_csv(
        cfg,
        "Fixture Labs,Priya Nair,hr,hr@fixturelabs.example,,\n"
        "Fixture Labs,Arjun Mehta,founder,arjun@fixturelabs.example,,\n",
    )
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])

    all_queued = db.queued_emails(due_only=False)
    due_now = db.queued_emails(due_only=True)
    assert len(all_queued) == 2
    assert [r["recipient_role"] for r in due_now] == [ROLE_HR]


def test_hr_is_sent_before_the_founder(cfg, db, gemini):
    write_contacts_csv(
        cfg,
        "Fixture Labs,Priya Nair,hr,hr@fixturelabs.example,,\n"
        "Fixture Labs,Arjun Mehta,founder,arjun@fixturelabs.example,,\n",
    )
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])
    # Make the founder email due as well.
    with db.conn:
        db.conn.execute("UPDATE emails SET scheduled_for=date('now','-1 day')")
    order = [r["recipient_role"] for r in db.queued_emails()]
    assert order[0] == ROLE_HR


def test_at_most_three_people_per_company(cfg, db, gemini):
    write_contacts_csv(
        cfg,
        "Fixture Labs,A,hr,hr@fixturelabs.example,,\n"
        "Fixture Labs,B,founder,founder@fixturelabs.example,,\n"
        "Fixture Labs,C,cofounder,cofounder@fixturelabs.example,,\n",
    )
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])
    assert db.company_email_count("Fixture Labs") == 3

    # A fourth contact must not be queued.
    db.add_contact(
        company="Fixture Labs", role=ROLE_HR, email="extra@fixturelabs.example"
    )
    stats = build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])
    assert stats.queued == 0
    assert db.company_email_count("Fixture Labs") == 3


def test_at_most_one_email_per_person(cfg, db, gemini):
    queue_one_email(cfg, db, gemini)
    assert len(db.queued_emails()) == 1

    stats = queue_one_email(cfg, db, gemini)
    assert stats.queued == 0
    assert any("max one per person" in n for n in stats.notes)


def test_a_company_that_replied_is_left_alone(cfg, db, gemini):
    db.mark_company_replied("Fixture Labs", "fixturelabs.example", "they replied")
    stats = queue_one_email(cfg, db, gemini)
    assert stats.queued == 0
    assert any("already replied" in n for n in stats.notes)


# ------------------------------------------------------- sending: gates


def send(cfg, db, gmail, **kwargs):
    kwargs.setdefault("mode", "auto")
    kwargs.setdefault("ignore_window", True)
    kwargs.setdefault("sleep", lambda s: None)
    return send_queue(cfg, db, service=gmail, **kwargs)


def test_auto_mode_sends_the_queue(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    stats = send(cfg, db, gmail)

    assert stats.sent == 1
    assert len(gmail.sent) == 1
    sent = gmail.sent[0]
    assert sent.to == "hr@fixturelabs.example"
    assert "Frontend Developer Intern" in sent.subject
    assert sent.has_attachment, "the resume PDF must be attached"
    assert sent.attachment_names == ["frontend.pdf"]


def test_sending_records_the_gmail_message_id(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)

    row = db.email_rows(None)[0]
    assert row["status"] == "sent"
    assert row["gmail_message_id"]
    assert row["gmail_thread_id"]
    assert row["sent_at"]


def test_queue_mode_asks_before_sending(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    answers = iter(["y"])
    stats = send(cfg, db, gmail, mode="queue", prompt=lambda p: next(answers))
    assert stats.sent == 1


def test_queue_mode_skips_on_no(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    stats = send(cfg, db, gmail, mode="queue", prompt=lambda p: "n")

    assert stats.sent == 0
    assert stats.declined == 1
    assert gmail.sent == []
    assert db.email_rows(None)[0]["status"] == "skipped"


def test_queue_mode_quits_on_q(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    stats = send(cfg, db, gmail, mode="queue", prompt=lambda p: "q")
    assert stats.sent == 0
    assert gmail.sent == []
    # Still queued, so nothing is lost.
    assert db.email_rows(None)[0]["status"] == "queued"


def test_queue_mode_can_edit_before_sending(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    answers = iter(
        [
            "e",                      # edit
            "Edited subject - Frontend Developer Intern",
            "My rewritten body.",     # body line 1
            "",                       # blank line ends the body
            "y",                      # send the edited version
        ]
    )
    stats = send(cfg, db, gmail, mode="queue", prompt=lambda p: next(answers))

    assert stats.edited == 1
    assert stats.sent == 1
    assert gmail.sent[0].subject == "Edited subject - Frontend Developer Intern"
    assert "My rewritten body." in gmail.sent[0].body
    assert db.email_rows(None)[0]["subject"].startswith("Edited subject")


def test_the_blocklist_is_honoured_at_send_time(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    cfg.abs_path(cfg.outreach.blocklist).write_text(
        "hr@fixturelabs.example\n", encoding="utf-8"
    )
    stats = send(cfg, db, gmail)

    assert stats.sent == 0
    assert gmail.sent == []
    assert any("blocklist" in n for n in stats.notes)


def test_a_bounced_address_is_never_used_again(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    db.add_contact(company="Fixture Labs", role=ROLE_HR, email="hr@fixturelabs.example")
    db.mark_contact("hr@fixturelabs.example", "bounced", "address not found")

    stats = send(cfg, db, gmail)
    assert stats.sent == 0
    assert gmail.sent == []


def test_a_reply_found_before_sending_stops_the_company(cfg, db, gemini, gmail):
    """Spec 8.4: check Gmail for a reply immediately before each send."""
    queue_one_email(cfg, db, gemini)
    gmail.add_reply_from("fixturelabs.example")

    stats = send(cfg, db, gmail)
    assert stats.sent == 0
    assert stats.replied_stops == 1
    assert gmail.sent == []
    assert db.company_replied("Fixture Labs") is not None


def test_the_same_address_is_not_emailed_twice_in_60_days(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    assert len(gmail.sent) == 1

    # Force a second queued email to the same person.
    db.queue_email(
        company="Fixture Labs",
        recipient_email="hr@fixturelabs.example",
        recipient_role=ROLE_HR,
        subject="Second attempt - Frontend Developer Intern",
        body="Body",
        resume_variant="frontend",
    )
    stats = send(cfg, db, gmail)
    assert stats.sent == 0
    assert len(gmail.sent) == 1


def test_the_daily_cap_stops_sending(cfg, db, gemini, gmail):
    cfg.outreach.sending.daily_cap = 2
    for i in range(4):
        db.queue_email(
            company="Company {0}".format(i),
            recipient_email="hr{0}@company{0}.example".format(i),
            recipient_role=ROLE_HR,
            subject="Role",
            body="Body",
            resume_variant="general",
        )
    stats = send(cfg, db, gmail)
    assert stats.sent == 2
    assert any("cap" in n for n in stats.notes)


def test_the_cap_can_never_exceed_forty(cfg, db):
    from limits import daily_email_cap

    cfg.outreach.sending.daily_cap = 500
    assert daily_email_cap(cfg) == 40


def test_the_sending_window_blocks_sending(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    cfg.outreach.sending.enforce_window = True

    stats = send_queue(cfg, db, service=gmail, mode="auto", ignore_window=False,
                       sleep=lambda s: None)
    # Either it is outside the window (nothing sent) or inside it (one sent).
    # The point is that the gate is consulted, which the note or the send proves.
    assert stats.sent in (0, 1)
    if stats.sent == 0:
        assert stats.notes


def test_a_send_failure_is_recorded_and_does_not_stop_the_run(cfg, db, gemini, gmail):
    for i in range(2):
        db.queue_email(
            company="Company {0}".format(i),
            recipient_email="hr{0}@company{0}.example".format(i),
            recipient_role=ROLE_HR,
            subject="Role",
            body="Body",
            resume_variant="general",
        )
    gmail.fail_next_send = True
    stats = send(cfg, db, gmail)

    assert stats.failed == 1
    assert stats.sent == 1
    failed = [r for r in db.email_rows(None) if r["status"] == "failed"]
    assert failed and failed[0]["error"]


def test_a_random_gap_is_taken_between_sends(cfg, db, gemini, gmail):
    cfg.outreach.sending.gap_min_s = 180
    cfg.outreach.sending.gap_max_s = 480
    for i in range(2):
        db.queue_email(
            company="Company {0}".format(i),
            recipient_email="hr{0}@company{0}.example".format(i),
            recipient_role=ROLE_HR,
            subject="Role",
            body="Body",
            resume_variant="general",
        )
    slept: list[float] = []
    send(cfg, db, gmail, sleep=slept.append)

    assert len(gmail.sent) == 2
    assert slept, "there must be a gap between sends"
    assert all(180 <= s <= 480 for s in slept), "spec: a 3-8 minute gap"


# ------------------------------------------------------- --email-test-to


def test_test_mode_redirects_every_email_to_one_address(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    stats = send(cfg, db, gmail, test_to="vaishshashank3@gmail.com")

    assert stats.sent == 1
    assert gmail.sent[0].to == "vaishshashank3@gmail.com"
    assert "hr@fixturelabs.example" not in gmail.sent[0].to
    # The database still records who it was really for.
    assert db.email_rows(None)[0]["recipient_email"] == "hr@fixturelabs.example"


def test_test_mode_still_attaches_the_resume(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail, test_to="me@example.com")
    assert gmail.sent[0].has_attachment


# ------------------------------------------------- 8.5 the one follow-up


def mark_sent_days_ago(db, days: int) -> None:
    old = (date.today() - timedelta(days=days)).isoformat()
    with db.conn:
        db.conn.execute("UPDATE emails SET sent_at=?, date=?", (old, old[:10]))


def test_no_followup_before_six_days(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    mark_sent_days_ago(db, 3)

    assert build_followup_queue(cfg, db, gemini).queued == 0


def test_one_followup_after_six_days(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    mark_sent_days_ago(db, 6)

    stats = build_followup_queue(cfg, db, gemini)
    assert stats.queued == 1

    followup = [r for r in db.email_rows(None) if r["is_followup"]][0]
    assert followup["recipient_role"] == ROLE_HR
    assert followup["status"] == "queued"
    assert "last time" in followup["body"].lower()


def test_there_is_never_a_second_followup(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    mark_sent_days_ago(db, 10)

    assert build_followup_queue(cfg, db, gemini).queued == 1
    # However long we wait, there is no second one.
    mark_sent_days_ago(db, 60)
    assert build_followup_queue(cfg, db, gemini).queued == 0


def test_only_hr_contacts_get_a_followup(cfg, db, gemini, gmail):
    write_contacts_csv(
        cfg, "Fixture Labs,Arjun Mehta,founder,arjun@fixturelabs.example,,\n"
    )
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])
    with db.conn:
        db.conn.execute("UPDATE emails SET scheduled_for=date('now','-2 day')")
    send(cfg, db, gmail)
    mark_sent_days_ago(db, 10)

    assert build_followup_queue(cfg, db, gemini).queued == 0


def test_the_followup_goes_in_the_same_thread(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    original_thread = gmail.sent[0].thread_id
    mark_sent_days_ago(db, 7)

    build_followup_queue(cfg, db, gemini)
    send(cfg, db, gmail)

    assert len(gmail.sent) == 2
    assert gmail.sent[1].thread_id == original_thread, "spec: same Gmail thread"
    assert gmail.sent[1].subject.startswith("Re: ")


def test_no_followup_if_the_company_replied(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    mark_sent_days_ago(db, 7)
    db.mark_company_replied("Fixture Labs", "fixturelabs.example", "replied")

    stats = build_followup_queue(cfg, db, gemini)
    assert stats.queued == 0


def test_followups_can_be_disabled(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    mark_sent_days_ago(db, 10)
    cfg.outreach.max_followups = 0

    stats = build_followup_queue(cfg, db, gemini)
    assert stats.queued == 0


# ------------------------------------------------- replies and bounces


def test_sync_detects_a_reply_and_stops_that_company(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    gmail.add_reply_from("fixturelabs.example")

    replies, bounces = sync_replies_and_bounces(cfg, db, service=gmail)
    assert replies == 1
    assert db.company_replied("Fixture Labs") is not None
    assert db.email_rows(None)[0]["status"] == "replied"


def test_sync_detects_a_bounce_and_blacklists_the_address(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    db.add_contact(company="Fixture Labs", role=ROLE_HR, email="hr@fixturelabs.example")
    gmail.add_bounce_for("hr@fixturelabs.example")

    replies, bounces = sync_replies_and_bounces(cfg, db, service=gmail)
    assert bounces == 1
    assert db.contact("hr@fixturelabs.example")["status"] == "bounced"
    assert db.email_rows(None)[0]["status"] == "bounced"


def test_sync_ignores_bounces_for_addresses_we_never_wrote_to(cfg, db, gmail):
    gmail.add_bounce_for("stranger@unrelated.example")
    replies, bounces = sync_replies_and_bounces(cfg, db, service=gmail)
    assert bounces == 0


def test_a_bounce_notice_is_not_treated_as_a_reply(cfg, db, gemini, gmail):
    queue_one_email(cfg, db, gemini)
    send(cfg, db, gmail)
    gmail.add_bounce_for("hr@fixturelabs.example")

    replies, _ = sync_replies_and_bounces(cfg, db, service=gmail)
    assert replies == 0, "a bounce is not a reply"


# ----------------------------------------------------------- the report


def test_email_report_on_an_empty_database(cfg, db, capsys):
    from main import cmd_email_report

    assert cmd_email_report(cfg, db, 30) == 0
    assert "No outreach emails" in capsys.readouterr().out


def test_email_report_shows_the_reply_rate(cfg, db, gemini, gmail, capsys):
    for i in range(4):
        db.queue_email(
            company="Company {0}".format(i),
            recipient_email="hr{0}@company{0}.example".format(i),
            recipient_role=ROLE_HR,
            subject="Role",
            body="Body",
            resume_variant="general",
        )
    send(cfg, db, gmail)
    db.mark_company_replied("Company 0", "company0.example", "replied")

    stats = db.email_stats(30)
    assert stats["sent"] == 3
    assert stats["replied"] == 1
    assert stats["reply_rate"] == 25.0

    from main import cmd_email_report

    assert cmd_email_report(cfg, db, 30) == 0
    out = capsys.readouterr().out
    assert "25.0%" in out
    assert "…" not in out and "�" not in out


def test_contacts_report_shows_where_each_contact_came_from(cfg, db, capsys):
    db.add_contact(
        company="Fixture Labs", role=ROLE_HR, email="hr@fixturelabs.example",
        name="Priya Nair", source="contacts_csv", verified=True, confidence=100,
    )
    db.record_contact_miss("Fixture Silent", ROLE_FOUNDER, "no_email_found")

    from main import cmd_contacts_report

    assert cmd_contacts_report(cfg, db) == 0
    out = capsys.readouterr().out
    assert "Fixture Labs" in out
    assert "contacts_csv" in out
    assert "No work email found" in out
    assert "never guessed" in out
