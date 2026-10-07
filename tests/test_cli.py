"""CLI wiring: the commands as `main.py` actually dispatches them.

The modules underneath are tested elsewhere. What these cover is the glue —
flag names, dispatch order, which commands need a network and which don't —
because a rename there breaks the tool without breaking any unit test.
"""
from __future__ import annotations

import pytest

from contacts import ROLE_FOUNDER, ROLE_HR
from main import build_parser


def args_for(*argv: str):
    """Parse real argv, so a renamed flag fails here."""
    return build_parser().parse_args(list(argv))


@pytest.fixture
def no_gmail(monkeypatch, gmail):
    """Every command gets the mock Gmail service instead of a real one."""
    import main

    monkeypatch.setattr(main, "_gmail_service", lambda cfg: (gmail, None))
    return gmail


def seed_contact_and_job(cfg, db):
    path = cfg.abs_path(cfg.outreach.contacts_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "company,name,role,email,source_url,notes\n"
        "Fixture Labs,Priya Nair,hr,hr@fixturelabs.example,,Met at a meetup\n",
        encoding="utf-8",
    )
    db.record(
        site="linkedin",
        company="Fixture Labs",
        role="Frontend Developer Intern",
        url="https://example.com/jobs/1",
        status="applied",
        apply_target="career_page",
        match_score=86,
        resume_variant="frontend",
        description="React and Tailwind internship, we ship weekly.",
    )


# ------------------------------------------------------------ flag parsing


def test_assist_is_the_default_with_no_flags():
    assert args_for().mode == "assist"


def test_every_email_flag_parses():
    assert args_for("--email-queue").email_queue is True
    assert args_for("--email-auto").email_auto is True
    assert args_for("--email-followups").email_followups is True
    assert args_for("--email-report").email_report is True
    assert args_for("--email-sync").email_sync is True
    assert args_for("--contacts-report").contacts_report is True
    assert args_for("--gmail-auth").gmail_auth is True
    assert args_for("--no-discover").no_discover is True
    assert args_for("--email-test-to", "me@example.com").email_test_to == "me@example.com"


def test_max_and_ignore_run_window_parse():
    args = args_for("--email-queue", "--max", "3", "--ignore-run-window")
    assert args.max == 3
    assert args.ignore_run_window is True


# -------------------------------------------------- offline vs online paths


@pytest.mark.parametrize(
    "flag",
    ["--check", "--report", "--export", "--login", "--email-report",
     "--contacts-report", "--gmail-auth"],
)
def test_offline_commands_do_not_demand_a_complete_config(flag, monkeypatch, tmp_path):
    """These must work before the profile is filled in or a key is set.

    Otherwise a beginner cannot run --check to find out what is wrong.
    """
    import yaml

    from config import load_config

    raw = yaml.safe_load(
        (__import__("pathlib").Path("config.yaml.example")).read_text(encoding="utf-8")
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    args = args_for(flag)
    offline = (
        args.check
        or args.report
        or args.export
        or args.login
        or args.email_report
        or args.contacts_report
        or args.gmail_auth
    )
    assert offline is True, flag + " should not require a complete config"
    # And loading non-strict really does succeed with placeholders present.
    assert load_config(path, strict=False) is not None


def test_the_apply_run_does_require_a_complete_config(tmp_path, monkeypatch):
    import yaml

    from config import load_config

    raw = yaml.safe_load(
        (__import__("pathlib").Path("config.yaml.example")).read_text(encoding="utf-8")
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    with pytest.raises(SystemExit):
        load_config(path, strict=True)


# ----------------------------------------------------------- --email-queue


def test_email_queue_builds_and_reviews(cfg, db, gemini, no_gmail, monkeypatch, capsys):
    """End to end through the CLI command, declining every email."""
    import main

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    seed_contact_and_job(cfg, db)

    import outreach

    monkeypatch.setattr(outreach, "input", lambda prompt="": "n", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    code = main.cmd_email_queue(cfg, db, args_for("--email-queue", "--no-discover"))
    assert code == 0

    out = capsys.readouterr().out
    assert "Outreach queue built" in out
    assert no_gmail.sent == [], "declining must send nothing"
    rows = db.email_rows(None)
    assert rows and rows[0]["status"] == "skipped"


def test_email_queue_with_nothing_to_do_is_not_an_error(cfg, db, gemini, monkeypatch, capsys):
    import main

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    assert main.cmd_email_queue(cfg, db, args_for("--email-queue", "--no-discover")) == 0
    assert "No outreach targets" in capsys.readouterr().out


def test_email_queue_respects_the_disabled_switch(cfg, db, gemini, monkeypatch, capsys):
    import main

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    cfg.outreach.enabled = False
    assert main.cmd_email_queue(cfg, db, args_for("--email-queue")) == 0
    assert "disabled" in capsys.readouterr().out


def test_no_discover_never_opens_a_browser(cfg, db, gemini, no_gmail, monkeypatch):
    """--no-discover must not launch Playwright at all."""
    import main

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    seed_contact_and_job(cfg, db)

    import browser.launcher as launcher

    def explode(*a, **k):
        raise AssertionError("--no-discover must not start a browser")

    monkeypatch.setattr(launcher, "BrowserSession", explode)
    assert main.cmd_email_queue(cfg, db, args_for("--email-queue", "--no-discover")) == 0


# ------------------------------------------------------------ --email-auto


def test_email_auto_sends_the_queue(cfg, db, gemini, no_gmail, monkeypatch, capsys):
    import main
    from outreach import Target, build_queue

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    seed_contact_and_job(cfg, db)
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])

    code = main.cmd_email_send(
        cfg, db, args_for("--email-auto", "--ignore-run-window"), mode="auto"
    )
    assert code == 0
    assert len(no_gmail.sent) == 1
    assert "Sending" in capsys.readouterr().out


def test_email_test_to_redirects_through_the_cli(cfg, db, gemini, no_gmail, monkeypatch, capsys):
    import main
    from outreach import Target, build_queue

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    seed_contact_and_job(cfg, db)
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])

    main.cmd_email_send(
        cfg,
        db,
        args_for(
            "--email-auto", "--ignore-run-window",
            "--email-test-to", "vaishshashank3@gmail.com",
        ),
        mode="auto",
    )
    assert no_gmail.sent[0].to == "vaishshashank3@gmail.com"
    assert "TEST MODE" in capsys.readouterr().out


def test_sending_outside_the_window_exits_two(cfg, db, gemini, no_gmail, monkeypatch, capsys):
    import main

    cfg.outreach.sending.enforce_window = True

    class Saturday:
        @staticmethod
        def now(tz=None):
            import datetime as real

            return real.datetime(2026, 10, 10, 10, 0, tzinfo=tz)

    import limits

    monkeypatch.setattr(limits, "datetime", Saturday)
    code = main.cmd_email_send(cfg, db, args_for("--email-auto"), mode="auto")
    assert code == 2
    assert "Not sending" in capsys.readouterr().out


def test_an_unauthorised_gmail_stops_before_any_review(cfg, db, monkeypatch, capsys):
    """The whole point of resolving the service up front."""
    import main

    import gmail_client

    def unconfigured(cfg_, interactive=True):
        raise gmail_client.GmailNotConfigured("no credentials.json here")

    monkeypatch.setattr(gmail_client, "get_service", unconfigured)
    code = main.cmd_email_send(
        cfg, db, args_for("--email-auto", "--ignore-run-window"), mode="auto"
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "Gmail is not ready" in out
    assert "--gmail-auth" in out


# ------------------------------------------------------- --email-followups


def test_email_followups_with_nothing_due(cfg, db, gemini, monkeypatch, capsys):
    import main

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    code = main.cmd_email_followups(cfg, db, args_for("--email-followups"))
    assert code == 0
    assert "Nothing is due a follow-up" in capsys.readouterr().out


def test_email_followups_queues_and_sends(cfg, db, gemini, no_gmail, monkeypatch):
    from datetime import date, timedelta

    import main
    from outreach import Target, build_queue, send_queue

    monkeypatch.setattr(main, "_gemini", lambda c: gemini)
    seed_contact_and_job(cfg, db)
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])
    send_queue(
        cfg, db, service=no_gmail, mode="auto", ignore_window=True, sleep=lambda s: None
    )
    assert len(no_gmail.sent) == 1

    old = (date.today() - timedelta(days=7)).isoformat()
    with db.conn:
        db.conn.execute("UPDATE emails SET sent_at=?, date=?", (old, old[:10]))

    code = main.cmd_email_followups(
        cfg, db, args_for("--email-followups", "--email-auto", "--ignore-run-window")
    )
    assert code == 0
    assert len(no_gmail.sent) == 2
    assert no_gmail.sent[1].subject.startswith("Re: ")


# ------------------------------------------------------------ --email-sync


def test_email_sync_reports_what_it_found(cfg, db, gemini, no_gmail, monkeypatch, capsys):
    import main
    from outreach import Target, build_queue, send_queue

    seed_contact_and_job(cfg, db)
    build_queue(cfg, db, gemini, [Target(company="Fixture Labs", job_role="Intern")])
    send_queue(
        cfg, db, service=no_gmail, mode="auto", ignore_window=True, sleep=lambda s: None
    )
    no_gmail.add_thread_reply(no_gmail.sent[0].thread_id)

    assert main.cmd_email_sync(cfg, db) == 0
    out = capsys.readouterr().out
    assert "1" in out and "repl" in out
    assert db.company_replied("Fixture Labs") is not None


# --------------------------------------------------------------- --check


def test_check_passes_on_a_complete_config(cfg, db, monkeypatch, capsys):
    import ai.gemini_client as gc

    from main import cmd_check

    monkeypatch.setattr(gc.GeminiClient, "ping", lambda self: '{"ok": true}')
    assert cmd_check(cfg, db) == 0
    out = capsys.readouterr().out
    assert "Everything checks out" in out
    assert "FAIL" not in out


def test_check_reports_gmail_as_not_set_up_without_failing(cfg, db, monkeypatch, capsys):
    """Gmail is only needed for outreach, so a missing token is not a FAIL."""
    import ai.gemini_client as gc

    from main import cmd_check

    monkeypatch.setattr(gc.GeminiClient, "ping", lambda self: '{"ok": true}')
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "./definitely-not-here.json")
    monkeypatch.setenv("GMAIL_TOKEN_PATH", "./definitely-not-here-token.json")

    assert cmd_check(cfg, db) == 0
    out = capsys.readouterr().out
    assert "Gmail" in out
    assert "gmail-auth" in out


def test_check_fails_when_hunter_is_on_without_a_key(cfg, db, monkeypatch, capsys):
    import ai.gemini_client as gc

    from main import cmd_check

    monkeypatch.setattr(gc.GeminiClient, "ping", lambda self: '{"ok": true}')
    cfg.outreach.use_hunter = True
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)

    assert cmd_check(cfg, db) == 1
    assert "HUNTER_API_KEY" in capsys.readouterr().out
