"""Spec compliance: the documented defaults and the non-negotiable safety rules.

These tests exist to catch drift. If someone changes a default in
config.yaml.example, loosens auto mode, or makes something send on its own,
one of these fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def example_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml.example").read_text(encoding="utf-8"))


# ------------------------------------------- section 1: the tech stack


def test_requirements_pin_the_expected_stack():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for package in ("playwright", "google-genai", "pydantic", "python-dotenv", "pyyaml"):
        assert package in text, package + " should be a dependency"


def test_default_model_is_a_gemini_flash_model():
    from config import DEFAULT_MODEL

    assert "flash" in DEFAULT_MODEL.lower()


def test_env_example_documents_the_key_and_the_model():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" in text
    assert "GEMINI_MODEL" in text


def test_browser_is_never_launched_headless():
    """The spec says always headed, so you can watch it."""
    source = (ROOT / "browser" / "launcher.py").read_text(encoding="utf-8")
    assert "headless=False" in source
    assert "headless=True" not in source


# -------------------------------- section 2: profile and resume variants


def test_example_config_ships_the_three_required_variants(example_config):
    names = {r["name"] for r in example_config["resumes"]}
    assert {"frontend", "data", "general"} <= names


def test_every_variant_declares_a_link_and_keywords(example_config):
    for variant in example_config["resumes"]:
        assert variant["pdf_path"]
        assert "public_link" in variant
        assert variant["focus_keywords"]


def test_profile_covers_every_field_the_spec_lists(example_config):
    profile = example_config["profile"]
    for key in (
        "name", "email", "phone", "location", "open_to_remote",
        "willing_to_relocate", "graduation_year", "degree", "college",
        "experience_level", "expected_salary", "notice_period", "linkedin",
        "github", "portfolio", "target_roles", "target_locations", "skills",
    ):
        assert key in profile, "profile." + key + " is missing from the example"


# ----------------------------------------- section 3: browser and login


def test_both_browser_modes_are_supported():
    from config import BrowserCfg

    assert BrowserCfg(mode="persistent").mode == "persistent"
    assert BrowserCfg(mode="cdp").mode == "cdp"
    with pytest.raises(Exception):
        BrowserCfg(mode="something-else")


def test_cdp_mode_uses_port_9222():
    from config import BrowserCfg

    assert "9222" in BrowserCfg().cdp_url


@pytest.mark.parametrize("script", ["start_chrome.bat", "start_chrome.sh"])
def test_chrome_scripts_use_a_dedicated_profile(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "9222" in text
    assert "chrome_bot_profile" in text
    assert "user-data-dir" in text


def test_no_password_is_ever_typed_or_stored():
    """Nothing in the project may fill a password field."""
    from ats.fields import CREDENTIAL_RE

    for label in ("Password", "Create a password", "OTP code", "CVV", "PIN"):
        assert CREDENTIAL_RE.search(label), label + " should be refused"

    for module in ("ats/fields.py", "browser/login.py", "runner.py"):
        source = (ROOT / module).read_text(encoding="utf-8")
        assert "input[type='password']" not in source or "fields.py" in module


def test_login_prompt_wording_matches_the_spec():
    source = (ROOT / "browser" / "login.py").read_text(encoding="utf-8")
    assert "Log in manually (email / Google / OTP), then press Enter." in source


# ------------------------------------------------ section 4: job sources


def test_every_required_source_exists():
    for module, cls in (
        ("sites.linkedin", "LinkedIn"),
        ("sites.internshala", "Internshala"),
        ("sites.naukri", "Naukri"),
        ("sites.indeed", "Indeed"),
    ):
        imported = __import__(module, fromlist=[cls])
        assert hasattr(imported, cls)


def test_every_required_ats_filler_exists():
    for module, cls in (
        ("ats.greenhouse", "GreenhouseFiller"),
        ("ats.lever", "LeverFiller"),
        ("ats.ashby", "AshbyFiller"),
        ("ats.workday", "WorkdayFiller"),
        ("ats.smartrecruiters", "SmartRecruitersFiller"),
        ("ats.generic", "GenericFiller"),
    ):
        imported = __import__(module, fromlist=[cls])
        assert hasattr(imported, cls)


def test_the_base_class_defines_the_three_required_methods():
    from sites.base import JobSource

    for method in ("search", "get_job_details", "apply"):
        assert callable(getattr(JobSource, method))


def test_sources_can_be_enabled_individually(example_config):
    for site in ("linkedin", "internshala", "naukri", "indeed"):
        assert "enabled" in example_config["sources"][site]


# ---------------------------------------------- section 6/7: thresholds


def test_match_threshold_defaults_to_70(example_config):
    from config import MatchingCfg

    assert MatchingCfg().match_threshold == 70
    assert example_config["matching"]["match_threshold"] == 70


def test_auto_threshold_defaults_to_80(example_config):
    from config import MatchingCfg

    assert MatchingCfg().auto_threshold == 80
    assert example_config["matching"]["auto_threshold"] == 80


def test_dedupe_window_defaults_to_30_days(example_config):
    from config import MatchingCfg

    assert MatchingCfg().dedupe_days == 30
    assert example_config["matching"]["dedupe_days"] == 30


def test_assist_is_the_default_mode():
    from main import build_parser

    assert build_parser().parse_args([]).mode == "assist"


@pytest.mark.parametrize(
    "flag,expected",
    [
        ("--assist", "assist"),
        ("--review", "review"),
        ("--dry-run", "dry-run"),
        ("--auto", "auto"),
    ],
)
def test_every_mode_flag_is_accepted(flag, expected):
    from main import build_parser

    assert build_parser().parse_args([flag]).mode == expected


def test_site_and_max_flags_are_accepted():
    from main import build_parser

    args = build_parser().parse_args(["--site", "linkedin,internshala", "--max", "5"])
    assert args.site == "linkedin,internshala"
    assert args.max == 5


def test_unknown_site_is_rejected_with_a_helpful_message(cfg):
    from main import selected_sites

    with pytest.raises(SystemExit) as excinfo:
        selected_sites(cfg, "linkedin,monster")
    assert "monster" in str(excinfo.value)


# ------------------------------------------- section 9: limits and safety


def test_daily_limit_defaults_match_the_spec(example_config):
    daily = example_config["limits"]["daily"]
    assert daily["linkedin"] == 15
    assert daily["naukri"] == 15
    assert daily["indeed"] == 20
    assert daily["internshala"] == 25
    assert daily["career_pages"] == 40


def test_run_window_is_9_to_21_ist_and_enforced_by_default(example_config):
    from config import RunWindow

    window = RunWindow()
    assert window.start_hour == 9
    assert window.end_hour == 21
    assert window.timezone == "Asia/Kolkata"
    assert window.enforce is True

    configured = example_config["limits"]["run_window"]
    assert configured["start_hour"] == 9
    assert configured["end_hour"] == 21
    assert configured["timezone"] == "Asia/Kolkata"
    assert configured["enforce"] is True


def test_delay_defaults_match_the_spec(example_config):
    from config import Delays

    delays = Delays()
    assert (delays.action_min_s, delays.action_max_s) == (3, 10)
    assert (delays.between_apps_min_s, delays.between_apps_max_s) == (45, 120)

    configured = example_config["limits"]["delays"]
    assert configured["action_min_s"] == 3
    assert configured["action_max_s"] == 10
    assert configured["between_apps_min_s"] == 45
    assert configured["between_apps_max_s"] == 120


def test_outside_the_window_the_run_is_refused(cfg, monkeypatch):
    import limits

    cfg.limits.run_window.enforce = True

    class FixedDatetime:
        @staticmethod
        def now(tz=None):
            import datetime as real

            return real.datetime(2026, 10, 5, 3, 0, tzinfo=tz)

    monkeypatch.setattr(limits, "datetime", FixedDatetime)
    ok, why = limits.now_in_window(cfg)
    assert ok is False
    assert "09:00" in why and "21:00" in why


def test_inside_the_window_the_run_is_allowed(cfg, monkeypatch):
    import limits

    cfg.limits.run_window.enforce = True

    class FixedDatetime:
        @staticmethod
        def now(tz=None):
            import datetime as real

            return real.datetime(2026, 10, 5, 14, 0, tzinfo=tz)

    monkeypatch.setattr(limits, "datetime", FixedDatetime)
    ok, _ = limits.now_in_window(cfg)
    assert ok is True


def test_the_window_can_be_disabled_in_config(cfg):
    from limits import now_in_window

    cfg.limits.run_window.enforce = False
    ok, why = now_in_window(cfg)
    assert ok is True
    assert "not enforced" in why


def test_gemini_retries_and_validates(cfg):
    """Invalid JSON must raise after retries rather than return junk."""
    from ai.gemini_client import GeminiClient, GeminiInvalidJSON
    from ai.schemas import MatchResult

    client = GeminiClient(cfg)

    class Models:
        calls = 0

        def generate_content(self, **kwargs):
            Models.calls += 1

            class Response:
                text = "not json at all"

            return Response()

    class FakeClient:
        models = Models()

    client._client = FakeClient()
    with pytest.raises(GeminiInvalidJSON):
        client._ask("prompt", MatchResult)
    assert Models.calls > 1, "the client should retry before giving up"


def test_an_invalid_score_is_rejected_by_the_schema():
    from pydantic import ValidationError

    from ai.schemas import MatchResult

    MatchResult(match_score=100, best_resume="general")
    with pytest.raises(ValidationError):
        MatchResult(match_score=140, best_resume="general")
    with pytest.raises(ValidationError):
        MatchResult(match_score=-1, best_resume="general")


def test_a_hallucinated_resume_name_falls_back_to_a_real_one(cfg, monkeypatch):
    from ai.gemini_client import GeminiClient
    from ai.schemas import MatchResult

    client = GeminiClient(cfg)
    monkeypatch.setattr(
        client,
        "_ask",
        lambda *a, **k: MatchResult(
            match_score=90, best_resume="a-variant-that-does-not-exist"
        ),
    )
    result = client.score_match(
        title="X", company="Y", location="Z", description="desc"
    )
    assert result.best_resume in {r.name for r in cfg.resumes}


# ------------------------------------------- cover letter word limit


def test_cover_letter_is_held_under_150_words():
    from ai.gemini_client import enforce_word_limit

    link = "https://drive.google.com/file/d/X/view"
    long_letter = (
        " ".join(["Sentence number {0} here.".format(i) for i in range(80)])
        + "\n\nResume: "
        + link
    )
    trimmed = enforce_word_limit(long_letter, 150, link)

    body = trimmed.split("Resume:")[0]
    assert len(body.split()) <= 150
    assert link in trimmed, "the resume link must survive trimming"
    assert body.rstrip().endswith(".")


def test_a_short_cover_letter_is_left_alone():
    from ai.gemini_client import enforce_word_limit

    letter = "Short and sweet.\n\nResume: https://example.com/r"
    assert enforce_word_limit(letter, 150, "https://example.com/r") == letter


# ------------------------------------- the absolute "never sends" rules


def _called_attributes(path: Path) -> set[str]:
    """Every attribute name that is actually *called* in this module.

    Parsed rather than grepped, so a docstring explaining that we never send
    doesn't count as sending.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_only_the_gmail_client_touches_the_send_endpoint():
    """Sending must stay in one module, behind one gate.

    This project now really does send email, so the guarantee changed shape:
    instead of "nothing sends", it is "exactly one module can send, and only
    when it is handed confirmed=True".
    """
    for path in sorted(ROOT.glob("*.py")):
        if path.name == "gmail_client.py":
            continue
        called = _called_attributes(path)
        assert "send" not in called, path.name + " must not call .send() itself"

    gmail_calls = _called_attributes(ROOT / "gmail_client.py")
    assert "send" in gmail_calls, "gmail_client is the module that sends"


def test_send_message_refuses_without_an_explicit_confirmation(cfg):
    """The one switch between "the user approved this" and "code decided to"."""
    from gmail_client import GmailSendRefused, send_message

    from tests.mock_gmail import MockGmailService

    service = MockGmailService()
    with pytest.raises(GmailSendRefused):
        send_message(
            cfg, to="someone@example.com", subject="s", body="b", service=service
        )
    assert service.sent == [], "nothing may leave without confirmed=True"


def test_send_message_refuses_an_invalid_address(cfg):
    from gmail_client import send_message

    from tests.mock_gmail import MockGmailService

    service = MockGmailService()
    for bad in ("", "not-an-address", None):
        with pytest.raises(ValueError):
            send_message(
                cfg, to=bad, subject="s", body="b", service=service, confirmed=True
            )
    assert service.sent == []


def test_only_the_sending_loop_passes_confirmed():
    """confirmed=True may be passed from exactly one place: outreach.send_queue.

    Parsed as real keyword arguments, so a docstring or an error message that
    mentions confirmed=True doesn't count.
    """
    import ast

    hits: list[str] = []
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "confirmed":
                    continue
                if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    hits.append("{0}:{1}".format(path.name, node.lineno))

    assert len(hits) == 1, "confirmed=True should be passed once, found: " + ", ".join(hits)
    assert hits[0].startswith("outreach.py"), hits[0]


def test_gmail_asks_for_send_and_read_only_nothing_wider():
    from gmail_client import SCOPES

    assert SCOPES == [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    text = (ROOT / "gmail_client.py").read_text(encoding="utf-8")
    # Never the scopes that allow deleting or rewriting the mailbox.
    for wider in ("gmail.modify", "mail.google.com", "gmail.settings"):
        assert wider not in text, "must never request " + wider


def test_oauth_is_never_started_unattended():
    """An unattended run must not block on a browser consent screen."""
    import inspect

    import gmail_client

    signature = inspect.signature(gmail_client.get_service)
    assert signature.parameters["interactive"].default is True
    source = inspect.getsource(gmail_client.get_service)
    assert "if not interactive:" in source
    assert "run_local_server" in source


def test_no_password_based_mail_path_exists():
    """Spec 8.1: OAuth only. No SMTP, no app password."""
    for path in sorted(ROOT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for banned in (
            "smtplib",
            "SMTP(",
            "smtp.login",
            "server.login",
            "GMAIL_PASSWORD",
            "app_password",
            "EMAIL_PASSWORD",
        ):
            assert banned not in text, path.name + " references " + banned


def test_the_daily_sending_cap_can_never_exceed_forty(cfg):
    from limits import daily_email_cap

    for attempted in (41, 100, 10_000):
        cfg.outreach.sending.daily_cap = attempted
        assert daily_email_cap(cfg) == 40


def test_the_default_sending_cap_is_twenty_five(example_config):
    from config import EmailSendingCfg

    assert EmailSendingCfg().daily_cap == 25
    assert example_config["outreach"]["sending"]["daily_cap"] == 25


def test_the_sending_window_defaults_to_weekday_mornings_ist(example_config):
    from config import EmailSendingCfg

    sending = EmailSendingCfg()
    assert (sending.start_hour, sending.start_minute) == (9, 30)
    assert (sending.end_hour, sending.end_minute) == (12, 30)
    assert sending.timezone == "Asia/Kolkata"
    assert sending.enforce_window is True
    assert sending.weekdays_only is True

    configured = example_config["outreach"]["sending"]
    assert configured["start_hour"] == 9
    assert configured["start_minute"] == 30
    assert configured["end_hour"] == 12
    assert configured["end_minute"] == 30
    assert configured["enforce_window"] is True


def test_weekends_are_refused(cfg, monkeypatch):
    import limits

    cfg.outreach.sending.enforce_window = True

    class Saturday:
        @staticmethod
        def now(tz=None):
            import datetime as real

            # 2026-10-10 is a Saturday.
            return real.datetime(2026, 10, 10, 10, 0, tzinfo=tz)

    monkeypatch.setattr(limits, "datetime", Saturday)
    ok, why = limits.email_window_now(cfg)
    assert ok is False
    assert "Monday to Friday" in why


def test_outside_the_morning_window_is_refused(cfg, monkeypatch):
    import limits

    cfg.outreach.sending.enforce_window = True

    class Afternoon:
        @staticmethod
        def now(tz=None):
            import datetime as real

            # 2026-10-08 is a Thursday, but 16:00 is outside 09:30-12:30.
            return real.datetime(2026, 10, 8, 16, 0, tzinfo=tz)

    monkeypatch.setattr(limits, "datetime", Afternoon)
    ok, why = limits.email_window_now(cfg)
    assert ok is False
    assert "09:30" in why and "12:30" in why


def test_inside_the_window_on_a_weekday_is_allowed(cfg, monkeypatch):
    import limits

    cfg.outreach.sending.enforce_window = True

    class ThursdayMorning:
        @staticmethod
        def now(tz=None):
            import datetime as real

            return real.datetime(2026, 10, 8, 10, 15, tzinfo=tz)

    monkeypatch.setattr(limits, "datetime", ThursdayMorning)
    ok, _ = limits.email_window_now(cfg)
    assert ok is True


def test_the_gap_between_sends_is_three_to_eight_minutes(example_config):
    from config import EmailSendingCfg

    sending = EmailSendingCfg()
    assert sending.gap_min_s == 180
    assert sending.gap_max_s == 480
    configured = example_config["outreach"]["sending"]
    assert configured["gap_min_s"] == 180
    assert configured["gap_max_s"] == 480


def test_exactly_one_followup_is_ever_allowed(example_config):
    """Spec 8.5: one follow-up. Never a second, whatever the config says."""
    from config import OutreachCfg

    assert OutreachCfg().max_followups == 1
    for attempted in (2, 5, 99):
        assert OutreachCfg(max_followups=attempted).max_followups == 1
    assert example_config["outreach"]["max_followups"] == 1


def test_the_followup_window_defaults_to_six_days(example_config):
    from config import OutreachCfg

    assert OutreachCfg().followup_after_days == 6
    assert example_config["outreach"]["followup_after_days"] == 6


def test_addresses_are_never_reused_inside_sixty_days(example_config):
    from config import OutreachCfg

    assert OutreachCfg().no_repeat_days == 60
    assert example_config["outreach"]["no_repeat_days"] == 60


def test_at_most_three_contacts_per_company(example_config):
    from config import OutreachCfg

    assert OutreachCfg().max_contacts_per_company == 3
    assert example_config["outreach"]["max_contacts_per_company"] == 3


def test_hunter_is_off_by_default_and_never_below_85(example_config):
    from config import OutreachCfg

    assert OutreachCfg().use_hunter is False
    assert example_config["outreach"]["use_hunter"] is False
    for attempted in (0, 50, 84):
        assert OutreachCfg(hunter_min_confidence=attempted).hunter_min_confidence == 85


def test_outreach_only_targets_jobs_at_seventy_or_above(example_config):
    from config import OutreachCfg

    assert OutreachCfg().match_threshold == 70
    assert example_config["outreach"]["match_threshold"] == 70


def test_addresses_are_never_pattern_guessed():
    """The anti-spam rule, checked against the module that finds addresses."""
    import inspect

    import contacts as module

    source = inspect.getsource(module)
    for pattern in ('"{0}@{1}"', "'{0}@{1}'", 'first + "@"', '"." + last'):
        assert pattern not in source, "looks like pattern guessing: " + pattern
    # And the rule is actually enforced.
    assert module.is_usable_work_email("someone@gmail.com") is False


def test_personal_addresses_are_refused_everywhere():
    from contacts import is_usable_work_email

    for address in ("a@gmail.com", "b@outlook.com", "c@yahoo.com", "d@icloud.com"):
        assert is_usable_work_email(address) is False


def test_email_statuses_cover_the_spec_list():
    from db import EMAIL_STATUSES

    assert set(EMAIL_STATUSES) == {
        "queued", "sent", "failed", "replied", "bounced", "skipped",
    }


def test_contact_roles_are_hr_founder_cofounder():
    from db import CONTACT_ROLES

    assert CONTACT_ROLES == ("hr", "founder", "cofounder")


def test_the_email_commands_exist():
    from main import build_parser

    parser = build_parser()
    for flag in (
        "--email-queue",
        "--email-auto",
        "--email-followups",
        "--email-report",
        "--email-sync",
        "--gmail-auth",
        "--contacts-report",
    ):
        args = parser.parse_args([flag])
        assert args is not None, flag


def test_email_test_to_takes_an_address():
    from main import build_parser

    args = build_parser().parse_args(["--email-test-to", "me@example.com"])
    assert args.email_test_to == "me@example.com"


def test_auto_mode_can_never_touch_a_job_board():
    from runner import NEVER_AUTO

    assert NEVER_AUTO == {"linkedin", "naukri", "indeed"}


def test_fillers_have_no_submit_capability():
    """Submitting is the runner's decision, so no filler may click submit."""
    for name in (
        "base.py", "greenhouse.py", "lever.py", "ashby.py",
        "workday.py", "smartrecruiters.py", "generic.py",
    ):
        source = (ROOT / "ats" / name).read_text(encoding="utf-8")
        assert "submit.click" not in source, name
        assert "submit_locator().click" not in source, name


# ------------------------------------------ section 11: output and logs


def test_log_filename_is_dated():
    import logging
    from datetime import date

    from config import load_config
    from main import setup_logging

    cfg = load_config(ROOT / "config.yaml.example", strict=False)
    path = setup_logging(cfg)
    assert path.name == "run_{0}.log".format(date.today().strftime("%Y-%m-%d"))
    logging.getLogger().handlers.clear()


def test_csv_export_columns_match_the_spec():
    from db import CSV_COLUMNS

    for column in (
        "date", "site", "company", "role", "url", "apply_target",
        "match_score", "resume_variant", "status", "error", "screenshot",
    ):
        assert column in CSV_COLUMNS


def test_every_status_the_spec_names_is_valid():
    from db import STATUSES

    assert set(STATUSES) == {"applied", "skipped", "failed", "needs_review", "drafted"}


def test_report_defaults_to_seven_days():
    from main import build_parser

    assert build_parser().parse_args(["--report"]).report_days == 7


# ------------------------------------- placeholders must never be submitted


def test_unreplaced_placeholders_are_rejected(tmp_path, monkeypatch):
    """A leftover "[https://github.com/you]" must never reach a real form."""
    import yaml

    from config import load_config

    raw = yaml.safe_load((ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    with pytest.raises(SystemExit) as excinfo:
        load_config(path, strict=True)
    message = str(excinfo.value)
    for field in ("profile.linkedin", "profile.github", "profile.notice_period"):
        assert field in message, field + " should be reported as unfilled"


def test_every_submittable_profile_field_is_validated():
    from config import _unfilled, Config

    cfg = Config(
        profile={
            "name": "A B",
            "email": "a@b.example",
            "phone": "1",
            "location": "X",
            "degree": "D",
            "college": "C",
            "linkedin": "[link]",
            "github": "[link]",
            "portfolio": "[link]",
            "expected_salary": "[pay]",
            "expected_stipend": "[pay]",
            "notice_period": "[when]",
            "skills": ["[skill]"],
            "target_roles": ["[role]"],
            "target_locations": ["[place]"],
        },
        resumes=[{"name": "general", "pdf_path": "./x.pdf", "public_link": "[link]"}],
    )
    reported = _unfilled(cfg)
    for field in (
        "profile.linkedin",
        "profile.github",
        "profile.portfolio",
        "profile.expected_salary",
        "profile.expected_stipend",
        "profile.notice_period",
        "profile.skills[0]",
        "profile.target_roles[0]",
        "profile.target_locations[0]",
        "resumes[0].public_link",
    ):
        assert field in reported, field


def test_a_dry_run_is_never_recorded_as_applied(cfg, db, gemini):
    """Regression: --url dry runs used to print "Outcome: applied"."""
    from models import ApplyTarget, FillReport, Job
    from runner import Runner
    from limits import RunBudget

    class NoSession:
        def new_page(self):
            raise AssertionError("not needed")

    class FakePage:
        url = "https://example.com/x"

        def screenshot(self, **kwargs):
            path = kwargs.get("path")
            if path:
                open(path, "wb").write(b"\x89PNG\r\n\x1a\n")

        def inner_text(self, selector, timeout=0):
            return "form"

    class FakeFiller:
        name = "greenhouse"

        def submit_locator(self):
            raise AssertionError("dry run must not look for a submit button")

        def has_next_button(self):
            return False

        def confirmed(self):
            return False

    class FakeMatch:
        match_score = 95
        best_resume = "general"
        reason = "fits"
        red_flags: list[str] = []

    runner = Runner(
        cfg, db, gemini, NoSession(), mode="dry-run",
        budget=RunBudget(cfg, db), skip_login=True,
    )
    job = Job(
        site="career_pages", title="T", company="C",
        url="https://example.com/x", apply_target=ApplyTarget.CAREER_PAGE,
    )
    outcome = runner.finish(
        FakePage(), FakeFiller(), job, FakeMatch(), FillReport(), allow_auto=True
    )
    assert outcome.status != "applied"
    assert "not submitted" in outcome.reason


# ----------------------------------- config that must not be decoration


def test_every_outreach_config_key_is_actually_read():
    """A setting nobody reads is a lie to the person editing the file.

    This caught retry_misses_after_days, follow_contact_page and
    weekdays_only, all of which were declared and ignored.
    """
    from config import EmailSendingCfg, OutreachCfg

    code = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "main.py", "outreach.py", "contacts.py", "limits.py",
            "gmail_client.py", "runner.py", "db.py",
        )
    )

    # Keys that are plumbing rather than behaviour.
    exempt = {"enabled", "outbox_dir", "hard_max"}

    for model in (OutreachCfg, EmailSendingCfg):
        for key in model.model_fields:
            if key in exempt or key == "sending":
                continue
            assert key in code, (
                "{0}.{1} is declared in config but never read".format(
                    model.__name__, key
                )
            )


def test_weekend_sending_can_be_enabled_deliberately(cfg, monkeypatch):
    import limits

    cfg.outreach.sending.enforce_window = True
    cfg.outreach.sending.weekdays_only = False

    class Saturday:
        @staticmethod
        def now(tz=None):
            import datetime as real

            return real.datetime(2026, 10, 10, 10, 0, tzinfo=tz)

    monkeypatch.setattr(limits, "datetime", Saturday)
    ok, _ = limits.email_window_now(cfg)
    assert ok is True, "weekdays_only: false should allow a Saturday send"


# ------------------------------------------- the timezone the windows need


def test_the_ist_timezone_actually_resolves():
    """Both safety windows are defined in IST.

    Windows ships no system timezone database, so without `tzdata` zoneinfo
    resolves nothing and every window silently becomes local time - which is
    wrong on any machine not already on IST. This caught exactly that.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Kolkata")
    assert zone is not None

    from datetime import datetime

    assert datetime.now(zone).tzinfo is not None


def test_tzdata_is_a_declared_dependency():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "tzdata" in text, "the timezone database must be installed, not assumed"


def test_a_missing_timezone_is_reported_not_swallowed():
    from limits import resolve_timezone

    tz, warning = resolve_timezone("Asia/Kolkata")
    assert tz is not None
    assert warning == ""

    tz, warning = resolve_timezone("Not/ARealZone")
    assert tz is None
    assert "local time" in warning
    assert "tzdata" in warning, "the message must say how to fix it"


def test_the_window_explanation_carries_the_timezone_warning(cfg):
    """A fallback to local time must be visible in what the user is shown."""
    from limits import email_window_now, now_in_window

    cfg.limits.run_window.timezone = "Not/ARealZone"
    cfg.outreach.sending.timezone = "Not/ARealZone"

    _, why = now_in_window(cfg)
    assert "local time" in why

    cfg.outreach.sending.enforce_window = True
    _, why = email_window_now(cfg)
    assert "local time" in why


def test_check_reports_the_timezone(cfg, db, capsys, monkeypatch):
    from main import cmd_check

    # Keep the Gemini row from making a network call.
    import ai.gemini_client as gc

    monkeypatch.setattr(gc.GeminiClient, "ping", lambda self: '{"ok": true}')
    cmd_check(cfg, db)
    out = capsys.readouterr().out
    assert "timezone" in out
    assert "Asia/Kolkata" in out
