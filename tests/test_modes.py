"""Stage 3: the four submit modes, and the terminal interaction they rely on.

The mode logic is what decides whether anything is ever sent, so it is tested
in isolation from page mechanics: a fake filler records whether Submit was
clicked. Real fillers finding real submit buttons is covered in test_fillers.
"""
from __future__ import annotations

import pytest

from interact import AssistResult, ask_yes_no, wait_for_user_submit
from limits import RunBudget
from models import ApplyTarget, FillReport, Job
from runner import Runner


class FakeSubmit:
    def __init__(self) -> None:
        self.clicks = 0

    def click(self, timeout: int = 0) -> None:
        self.clicks += 1

    def evaluate(self, script: str) -> bool:
        return True

    def inner_text(self) -> str:
        return "Submit Application"


class FakeFiller:
    """Stands in for an ATSFiller so mode logic can be tested on its own."""

    def __init__(
        self,
        *,
        name: str = "greenhouse",
        submit: FakeSubmit | None = None,
        confirm_after_click: bool = True,
        multistep: bool = False,
    ) -> None:
        self.name = name
        self.submit = submit if submit is not None else FakeSubmit()
        self.confirm_after_click = confirm_after_click
        self.multistep = multistep

    def submit_locator(self):
        return self.submit

    def has_next_button(self) -> bool:
        return self.multistep

    def confirmed(self) -> bool:
        return self.confirm_after_click and self.submit.clicks > 0


class FakePage:
    """Enough of a page for screenshots and waits."""

    def __init__(self) -> None:
        self.url = "https://boards.greenhouse.io/fixture/jobs/1"
        self.waits = 0

    def screenshot(self, **kwargs) -> None:
        path = kwargs.get("path")
        if path:
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n")

    def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1

    def inner_text(self, selector: str, timeout: int = 0) -> str:
        return "Thank you for applying" if self.waits else "form"


def make_job(site: str = "career_pages", target=ApplyTarget.CAREER_PAGE) -> Job:
    return Job(
        site=site,
        title="Frontend Engineer Intern",
        company="Fixture Labs",
        url="https://boards.greenhouse.io/fixture/jobs/1",
        location="Noida",
        description="React internship.",
        apply_target=target,
        ats="greenhouse",
    )


class FakeMatch:
    def __init__(self, score: int = 90, resume: str = "frontend") -> None:
        self.match_score = score
        self.best_resume = resume
        self.reason = "good fit"
        self.red_flags: list[str] = []


def make_runner(cfg, db, gemini, mode: str) -> Runner:
    class NoSession:
        def new_page(self):
            return FakePage()

    return Runner(
        cfg, db, gemini, NoSession(), mode=mode,
        budget=RunBudget(cfg, db), skip_login=True,
    )


def clean_report(ats: str = "greenhouse") -> FillReport:
    r = FillReport(ats=ats)
    r.note("email", "asha.verma@example.com")
    r.resume_uploaded = True
    r.resume_link_placed = True
    return r


# ----------------------------------------------------------------- dry run


def test_dry_run_never_clicks_submit(cfg, db, gemini):
    runner = make_runner(cfg, db, gemini, "dry-run")
    filler = FakeFiller()
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    assert filler.submit.clicks == 0
    assert "not submitted" in outcome.reason


def test_dry_run_screenshots_the_form(cfg, db, gemini):
    runner = make_runner(cfg, db, gemini, "dry-run")
    outcome = runner.finish(
        FakePage(), FakeFiller(), make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    assert outcome.screenshot, "a dry run must screenshot every form"


# ------------------------------------------------------------------- auto


def test_auto_submits_a_high_scoring_career_page_job(cfg, db, gemini):
    runner = make_runner(cfg, db, gemini, "auto")
    filler = FakeFiller()
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(score=90), clean_report(), allow_auto=True
    )
    assert filler.submit.clicks == 1
    assert outcome.status == "applied"


@pytest.mark.parametrize("site", ["linkedin", "naukri", "indeed"])
def test_auto_never_submits_on_a_job_board(cfg, db, gemini, site):
    runner = make_runner(cfg, db, gemini, "auto")
    filler = FakeFiller()
    job = make_job(site=site, target=ApplyTarget.BOARD)
    outcome = runner.finish(
        FakePage(), filler, job, FakeMatch(score=99), clean_report(), allow_auto=False
    )
    assert filler.submit.clicks == 0
    assert outcome.status == "needs_review"
    assert site in outcome.reason


def test_auto_refuses_below_the_auto_threshold(cfg, db, gemini):
    runner = make_runner(cfg, db, gemini, "auto")
    filler = FakeFiller()
    # Above match_threshold (70) but below auto_threshold (80).
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(score=75), clean_report(), allow_auto=True
    )
    assert filler.submit.clicks == 0
    assert outcome.status == "needs_review"
    assert "80" in outcome.reason


def test_auto_refuses_a_form_that_needs_review(cfg, db, gemini):
    runner = make_runner(cfg, db, gemini, "auto")
    filler = FakeFiller()
    report = clean_report()
    report.flag("low-confidence answer: do you have a car?")
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(score=95), report, allow_auto=True
    )
    assert filler.submit.clicks == 0
    assert outcome.status == "needs_review"


def test_auto_refuses_when_no_submit_button_is_found(cfg, db, gemini):
    runner = make_runner(cfg, db, gemini, "auto")
    filler = FakeFiller()
    filler.submit_locator = lambda: None
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(score=95), clean_report(), allow_auto=True
    )
    assert outcome.status == "needs_review"
    assert "submit button" in outcome.reason


# ----------------------------------------------------------------- review


def test_review_submits_on_yes(cfg, db, gemini, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "ask_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(runner_module, "beep", lambda: None)
    runner = make_runner(cfg, db, gemini, "review")
    filler = FakeFiller()
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    assert filler.submit.clicks == 1
    assert outcome.status == "applied"


def test_review_skips_on_no(cfg, db, gemini, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "ask_yes_no", lambda *a, **k: False)
    monkeypatch.setattr(runner_module, "beep", lambda: None)
    runner = make_runner(cfg, db, gemini, "review")
    filler = FakeFiller()
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    assert filler.submit.clicks == 0
    assert outcome.status == "skipped"
    assert "declined" in outcome.reason


def test_review_on_a_job_board_is_allowed(cfg, db, gemini, monkeypatch):
    """Unlike auto, review mode may submit on a board - you said yes."""
    import runner as runner_module

    monkeypatch.setattr(runner_module, "ask_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(runner_module, "beep", lambda: None)
    runner = make_runner(cfg, db, gemini, "review")
    filler = FakeFiller(name="linkedin")
    job = make_job(site="linkedin", target=ApplyTarget.BOARD)
    outcome = runner.finish(
        FakePage(), filler, job, FakeMatch(), clean_report("linkedin"), allow_auto=False
    )
    assert filler.submit.clicks == 1
    assert outcome.status == "applied"


# ----------------------------------------------------------------- assist


def test_assist_logs_applied_when_you_submit(cfg, db, gemini, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "beep", lambda: None)
    monkeypatch.setattr(runner_module, "highlight", lambda loc: True)
    # Simulate the user clicking Submit in the browser.
    monkeypatch.setattr(
        runner_module, "wait_for_user_submit", lambda **kw: AssistResult.SUBMITTED
    )
    runner = make_runner(cfg, db, gemini, "assist")
    filler = FakeFiller()
    outcome = runner.finish(
        FakePage(), filler, make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    # The bot must never click in assist mode.
    assert filler.submit.clicks == 0
    assert outcome.status == "applied"


def test_assist_skips_when_you_press_s(cfg, db, gemini, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "beep", lambda: None)
    monkeypatch.setattr(runner_module, "highlight", lambda loc: True)
    monkeypatch.setattr(
        runner_module, "wait_for_user_submit", lambda **kw: AssistResult.SKIPPED
    )
    runner = make_runner(cfg, db, gemini, "assist")
    outcome = runner.finish(
        FakePage(), FakeFiller(), make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    assert outcome.status == "skipped"
    assert "pressed s" in outcome.reason


def test_assist_flags_review_on_timeout(cfg, db, gemini, monkeypatch):
    import runner as runner_module

    monkeypatch.setattr(runner_module, "beep", lambda: None)
    monkeypatch.setattr(runner_module, "highlight", lambda loc: True)
    monkeypatch.setattr(
        runner_module, "wait_for_user_submit", lambda **kw: AssistResult.TIMEOUT
    )
    runner = make_runner(cfg, db, gemini, "assist")
    outcome = runner.finish(
        FakePage(), FakeFiller(), make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    assert outcome.status == "needs_review"


def test_assist_highlights_the_submit_button(cfg, db, gemini, monkeypatch):
    import runner as runner_module

    highlighted = []
    monkeypatch.setattr(runner_module, "beep", lambda: None)
    monkeypatch.setattr(
        runner_module, "highlight", lambda loc: highlighted.append(loc) or True
    )
    monkeypatch.setattr(
        runner_module, "wait_for_user_submit", lambda **kw: AssistResult.SKIPPED
    )
    runner = make_runner(cfg, db, gemini, "assist")
    filler = FakeFiller()
    runner.finish(
        FakePage(), filler, make_job(), FakeMatch(), clean_report(), allow_auto=True
    )
    assert highlighted == [filler.submit]


# --------------------------------------------------- interact.py internals


def test_wait_returns_submitted_as_soon_as_the_page_confirms():
    assert (
        wait_for_user_submit(is_confirmed=lambda: True, timeout_s=5, poll_s=0.01)
        == AssistResult.SUBMITTED
    )


def test_wait_returns_skipped_when_s_is_pressed():
    class Keys:
        def __init__(self) -> None:
            self.sent = False

        def poll(self):
            if not self.sent:
                self.sent = True
                return "s"
            return None

    result = wait_for_user_submit(
        is_confirmed=lambda: False, timeout_s=5, poll_s=0.01, keys=Keys()
    )
    assert result == AssistResult.SKIPPED


def test_wait_times_out_without_input():
    result = wait_for_user_submit(
        is_confirmed=lambda: False, timeout_s=0.2, poll_s=0.05, announce=lambda *a: None
    )
    assert result == AssistResult.TIMEOUT


def test_wait_survives_a_confirmation_check_that_raises():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("page closed")
        return True

    assert (
        wait_for_user_submit(is_confirmed=flaky, timeout_s=5, poll_s=0.01)
        == AssistResult.SUBMITTED
    )


@pytest.mark.parametrize(
    "typed,expected",
    [("y", True), ("yes", False), ("n", False), ("no", False)],
)
def test_ask_yes_no_reads_the_answer(monkeypatch, typed, expected):
    monkeypatch.setattr("builtins.input", lambda prompt="": typed)
    if typed == "yes":
        expected = True
    assert ask_yes_no("Submit?", default=False) is expected


def test_ask_yes_no_uses_the_default_on_empty_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert ask_yes_no("Submit?", default=False) is False
    assert ask_yes_no("Submit?", default=True) is True
