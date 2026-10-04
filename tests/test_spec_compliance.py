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


def test_nothing_in_the_project_sends_email():
    """Only drafts().create() is ever called. No send path may exist."""
    for path in sorted(ROOT.glob("*.py")):
        called = _called_attributes(path)
        assert "send" not in called, path.name + " calls .send()"
        assert "send_message" not in called, path.name + " calls .send_message()"

    gmail_calls = _called_attributes(ROOT / "gmail_client.py")
    assert "drafts" in gmail_calls, "the draft API is how this module works"
    assert "messages" not in gmail_calls, "messages() is the send path"

    gmail = (ROOT / "gmail_client.py").read_text(encoding="utf-8")
    assert "gmail.send" not in gmail, "the send scope must never be requested"


def test_gmail_requests_only_the_compose_scope():
    from gmail_client import SCOPES

    assert SCOPES == ["https://www.googleapis.com/auth/gmail.compose"]


def test_gmail_is_off_by_default(example_config):
    from config import EmailCfg

    assert EmailCfg().gmail_api is False
    assert example_config["email"]["gmail_api"] is False


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
