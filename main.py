"""Personal job and internship application assistant.

Run `python main.py --help` for the full flag list, or start with:

    python main.py --check            verify config, database and Gemini key
    python main.py --login            open each enabled site and sign in by hand
    python main.py --dry-run          fill forms but never submit
    python main.py --report           last 7 days of activity
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from config import Config, load_config
from db import Database, open_db

ALL_SITES = ["linkedin", "internshala", "naukri", "indeed"]
MODES = ("assist", "review", "dry-run", "auto")


def _make_console() -> Console:
    """Windows terminals often default to cp1252, which can't encode the
    characters rich uses for box corners and truncation. Force UTF-8 and never
    crash a run over an unprintable character."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return Console(soft_wrap=False)


console = _make_console()


def clip(text: str, width: int) -> str:
    """Hard-truncate with ASCII, so no multi-byte ellipsis reaches the terminal."""
    s = " ".join((text or "").split())
    return s if len(s) <= width else s[: max(1, width - 3)] + "..."


# --------------------------------------------------------------------- logging


def setup_logging(cfg: Config, verbose: bool = False) -> Path:
    log_dir = cfg.abs_path(cfg.paths.logs_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run_{0}.log".format(date.today().strftime("%Y-%m-%d"))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(stream)

    # Playwright is extremely chatty at DEBUG.
    logging.getLogger("playwright").setLevel(logging.WARNING)
    return log_path


# ------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python main.py",
        description="Find, score and fill job/internship applications. You stay in control.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--assist",
        action="store_const",
        const="assist",
        dest="mode",
        help="DEFAULT. Fill the form, highlight Submit, beep, then wait for you to click it.",
    )
    mode.add_argument(
        "--review",
        action="store_const",
        const="review",
        dest="mode",
        help="Fill the form, show a summary, ask y/n; the bot clicks Submit on 'y'.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_const",
        const="dry-run",
        dest="mode",
        help="Fill but never submit. Screenshots every form.",
    )
    mode.add_argument(
        "--auto",
        action="store_const",
        const="auto",
        dest="mode",
        help="Submit without asking. Career-page jobs only, score >= auto_threshold. "
        "Never used on LinkedIn, Naukri or Indeed.",
    )

    p.add_argument(
        "--site",
        default="",
        help="Comma-separated subset of sources, e.g. --site linkedin,internshala",
    )
    p.add_argument("--max", type=int, default=0, help="Overall cap on applications this run")
    p.add_argument("--config", default="config.yaml", help="Path to your config file")
    p.add_argument("--verbose", action="store_true", help="Debug logging in the terminal")
    p.add_argument(
        "--ignore-run-window",
        action="store_true",
        help="Bypass the 09:00-21:00 check for this run only (for testing)",
    )
    p.add_argument(
        "--no-login",
        action="store_true",
        help="Skip the login check. Only useful against local test fixtures.",
    )
    p.add_argument(
        "--url",
        default="",
        help="Fill one specific application URL instead of searching. "
        "Accepts a file:// path, so it works against tests/fixtures/.",
    )
    p.add_argument(
        "--followups",
        action="store_true",
        help="Draft follow-ups for applications with no reply, then exit",
    )

    tasks = p.add_argument_group("one-off tasks")
    tasks.add_argument(
        "--check",
        action="store_true",
        help="Verify config, resumes, database and the Gemini API key, then exit",
    )
    tasks.add_argument(
        "--login",
        action="store_true",
        help="Open each enabled site and pause for manual login, then exit",
    )
    tasks.add_argument("--report", action="store_true", help="Show the last 7 days and exit")
    tasks.add_argument(
        "--report-days", type=int, default=7, help="How many days --report covers"
    )
    tasks.add_argument(
        "--export", action="store_true", help="Write applications.csv and exit"
    )

    p.set_defaults(mode="assist")
    return p


def selected_sites(cfg: Config, raw: str) -> list[str]:
    if raw.strip():
        wanted = [s.strip().lower() for s in raw.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in ALL_SITES]
        if unknown:
            raise SystemExit(
                "Unknown --site value(s): {0}\nValid sources: {1}".format(
                    ", ".join(unknown), ", ".join(ALL_SITES)
                )
            )
        return wanted
    return [s for s in ALL_SITES if cfg.source(s).enabled]


# ------------------------------------------------------------------ --check


def cmd_check(cfg: Config, db: Database) -> int:
    table = Table(title="Setup check", show_lines=False)
    table.add_column("Item")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    ok = True

    def row(item: str, good: bool, detail: str) -> None:
        nonlocal ok
        if not good:
            ok = False
        table.add_row(item, "[green]OK[/]" if good else "[red]FAIL[/]", detail)

    row("config.yaml", True, "loaded for " + cfg.profile.name)
    row("profile email", "@" in cfg.profile.email, cfg.profile.email)
    row("profile phone", bool(cfg.profile.phone.strip()), cfg.profile.phone)
    row(
        "target roles",
        bool(cfg.profile.target_roles),
        ", ".join(cfg.profile.target_roles) or "none set",
    )

    for r in cfg.resumes:
        row(
            "resume: " + r.name,
            r.exists(),
            str(r.abs_pdf_path) if r.exists() else "missing file " + str(r.abs_pdf_path),
        )
        link_ok = r.public_link.startswith("http")
        row(
            "resume link: " + r.name,
            link_ok,
            r.public_link if link_ok else "not a URL - set a 'anyone with link' Drive link",
        )

    row("database", True, str(db.path))
    row(
        "browser mode",
        True,
        cfg.browser.mode
        + (
            " -> " + str(cfg.abs_path(cfg.browser.persistent_profile_dir))
            if cfg.browser.mode == "persistent"
            else " -> " + cfg.browser.cdp_url
        ),
    )

    enabled = [s for s in ALL_SITES if cfg.source(s).enabled]
    row("sources enabled", bool(enabled), ", ".join(enabled) or "none enabled in config.yaml")

    # Gemini last: it's the only check that costs a network call.
    from ai.gemini_client import GeminiClient, GeminiUnavailable

    try:
        reply = GeminiClient(cfg).ping()
        row("Gemini API", True, cfg.gemini_model + " replied: " + reply[:60])
    except GeminiUnavailable as exc:
        row("Gemini API", False, str(exc))
    except Exception as exc:
        row("Gemini API", False, "{0}: {1}".format(type(exc).__name__, str(exc)[:160]))

    console.print(table)
    if ok:
        console.print("\n[green]Everything checks out.[/] Next: python main.py --login\n")
    else:
        console.print(
            "\n[red]Fix the FAIL rows above before running applications.[/]\n"
            "The Setup section of README.md walks through each one.\n"
        )
    return 0 if ok else 1


# ------------------------------------------------------------------ --login


def cmd_login(cfg: Config, sites: list[str]) -> int:
    from browser.launcher import BrowserSession
    from browser.login import AUTH, ensure_logged_in

    targets = [s for s in sites if s in AUTH]
    if not targets:
        console.print(
            "[yellow]No account-based sources selected.[/] "
            "Enable linkedin / internshala / naukri / indeed in config.yaml, "
            "or pass --site."
        )
        return 0

    console.print(
        "\nOpening a browser window. Sign in to each site by hand when prompted.\n"
        "[dim]This program never sees, types or stores your password.[/]\n"
    )
    failures: list[str] = []
    with BrowserSession(cfg) as session:
        page = session.new_page()
        for site in targets:
            if not ensure_logged_in(page, site):
                failures.append(site)

    if failures:
        console.print("[yellow]Still signed out:[/] " + ", ".join(failures))
        return 1
    console.print("[green]All selected sites are signed in.[/]")
    return 0


# ----------------------------------------------------------------- --report


def cmd_report(cfg: Config, db: Database, days: int) -> int:
    rows = db.recent(days)
    if not rows:
        console.print(
            "[yellow]No activity in the last {0} days.[/] "
            "The database is at {1}".format(days, db.path)
        )
        return 0

    per_site: dict[str, dict[str, int]] = {}
    for r in rows:
        bucket = per_site.setdefault(
            r["site"],
            {"found": 0, "applied": 0, "skipped": 0, "failed": 0, "needs_review": 0, "drafted": 0},
        )
        bucket["found"] += 1
        if r["status"] in bucket:
            bucket[r["status"]] += 1

    summary = Table(title="Last {0} days by source".format(days))
    summary.add_column("Source")
    for col in ("found", "applied", "drafted", "needs_review", "skipped", "failed"):
        summary.add_column(col.replace("_", " "), justify="right")
    totals = {k: 0 for k in ("found", "applied", "drafted", "needs_review", "skipped", "failed")}
    for site, b in sorted(per_site.items()):
        summary.add_row(
            site,
            str(b["found"]),
            str(b["applied"]),
            str(b["drafted"]),
            str(b["needs_review"]),
            str(b["skipped"]),
            str(b["failed"]),
        )
        for k in totals:
            totals[k] += b[k]
    summary.add_row(
        "[bold]TOTAL[/]",
        *["[bold]{0}[/]".format(totals[k]) for k in
          ("found", "applied", "drafted", "needs_review", "skipped", "failed")],
    )
    console.print(summary)

    detail = Table(title="Activity detail", show_lines=False)
    detail.add_column("date", no_wrap=True, min_width=10)
    detail.add_column("site", no_wrap=True, min_width=11)
    detail.add_column("company", no_wrap=True)
    detail.add_column("role", no_wrap=True)
    detail.add_column("score", justify="right", no_wrap=True)
    detail.add_column("resume", no_wrap=True)
    detail.add_column("status", no_wrap=True)
    for r in rows[:60]:
        status = r["status"]
        colour = {
            "applied": "green",
            "drafted": "cyan",
            "needs_review": "yellow",
            "skipped": "dim",
            "failed": "red",
        }.get(status, "white")
        detail.add_row(
            r["date"],
            r["site"],
            clip(r["company"], 24),
            clip(r["role"], 30),
            str(r["match_score"] if r["match_score"] is not None else "-"),
            r["resume_variant"] or "-",
            "[{0}]{1}[/]".format(colour, status),
        )
    console.print(detail)
    if len(rows) > 60:
        console.print("[dim]... and {0} more rows. See applications.csv[/]".format(len(rows) - 60))

    skipped_reasons = [r for r in rows if r["status"] == "skipped" and r["error"]]
    if skipped_reasons:
        reasons = Table(title="Why jobs were skipped")
        reasons.add_column("Company / role", overflow="fold")
        reasons.add_column("Reason", overflow="fold")
        for r in skipped_reasons[:20]:
            reasons.add_row(
                clip("{0} - {1}".format(r["company"], r["role"]), 45),
                clip(r["error"], 90),
            )
        console.print(reasons)
    return 0


# -------------------------------------------------------------------- export


def cmd_export(cfg: Config, db: Database) -> int:
    out = db.export_csv(cfg.abs_path(cfg.paths.applications_csv))
    console.print("[green]Exported[/] " + str(out))
    return 0


# ----------------------------------------------------------------- followups


def cmd_followups(cfg: Config, db: Database) -> int:
    from ai.gemini_client import GeminiClient
    from email_drafts import build_followups

    rows = build_followups(cfg, db, GeminiClient(cfg))
    if not rows:
        console.print(
            "[yellow]Nothing is due a follow-up[/] "
            "(applications become due {0} days after applying).".format(
                cfg.followups.days_after
            )
        )
        return 0
    table = Table(title="Follow-ups queued (not sent)")
    table.add_column("applied", no_wrap=True)
    table.add_column("company", max_width=24)
    table.add_column("role", max_width=28)
    table.add_column("channel", no_wrap=True)
    for r in rows:
        table.add_row(r["applied_on"], r["company"], r["role"], r["channel"])
    console.print(table)
    console.print(
        "Written to [bold]{0}[/]. Nothing was sent - review and send them yourself.".format(
            cfg.abs_path(cfg.followups.csv_path)
        )
    )
    return 0


# ---------------------------------------------------------------- the run


def print_run_summary(cfg: Config, db: Database, stats) -> None:
    table = Table(title="This run")
    table.add_column("Source", no_wrap=True)
    for col in ("found", "applied", "drafted", "needs review", "skipped", "failed"):
        table.add_column(col, justify="right", no_wrap=True)

    for site, s in sorted(stats.per_site.items()):
        table.add_row(
            site,
            str(s.found),
            str(s.applied),
            str(s.drafted),
            str(s.needs_review),
            str(s.skipped),
            str(s.failed),
        )
    t = stats.totals()
    table.add_row(
        "[bold]TOTAL[/]",
        "[bold]{0}[/]".format(t.found),
        "[bold]{0}[/]".format(t.applied),
        "[bold]{0}[/]".format(t.drafted),
        "[bold]{0}[/]".format(t.needs_review),
        "[bold]{0}[/]".format(t.skipped),
        "[bold]{0}[/]".format(t.failed),
    )
    console.print(table)

    for note in stats.notes:
        console.print("[yellow]note:[/] " + note)

    out = db.export_csv(cfg.abs_path(cfg.paths.applications_csv))
    console.print("Tracking exported to [bold]{0}[/]".format(out))


def cmd_run(cfg: Config, db: Database, args, sites: list[str], log_path: Path) -> int:
    from ai.gemini_client import GeminiClient
    from browser.launcher import BrowserSession
    from limits import RunBudget, now_in_window
    from runner import Runner

    if not args.ignore_run_window:
        ok, why = now_in_window(cfg)
        if not ok:
            console.print("[red]Outside the run window.[/] " + why)
            return 2
        logging.info("Run window: %s", why)

    if args.mode == "dry-run":
        console.print("[cyan]DRY RUN[/] - forms get filled and screenshotted, never submitted.")
    elif args.mode == "auto":
        console.print(
            "[yellow]AUTO MODE[/] - career-page jobs scoring >= {0} will be submitted "
            "without asking. LinkedIn, Naukri and Indeed are never auto-submitted.".format(
                cfg.matching.auto_threshold
            )
        )

    gemini = GeminiClient(cfg)
    budget = RunBudget(cfg, db, run_max=args.max)

    usable, rejected = budget.usable_sites(sites)
    for site, reason in rejected.items():
        console.print("[yellow]skipping {0}:[/] {1}".format(site, reason))

    if args.url:
        return cmd_run_single_url(cfg, db, args, gemini, budget)

    if not usable:
        console.print(
            "[red]No usable sources.[/] Enable some under `sources:` in config.yaml, "
            "or pass --site."
        )
        return 1

    logging.info(
        "Mode=%s sites=%s max=%s log=%s",
        args.mode,
        ",".join(usable),
        args.max or "unlimited",
        log_path,
    )

    with BrowserSession(cfg) as session:
        runner = Runner(
            cfg,
            db,
            gemini,
            session,
            mode=args.mode,
            budget=budget,
            skip_login=args.no_login,
        )
        stats = runner.run(usable)

    print_run_summary(cfg, db, stats)
    console.print("Log: [bold]{0}[/]".format(log_path))
    return 0


def cmd_run_single_url(cfg: Config, db: Database, args, gemini, budget) -> int:
    """Fill one application URL. The way to exercise fillers against fixtures."""
    from ats import detect as ats_detect
    from ats.fields import FillContext
    from browser.launcher import BrowserSession
    from models import ApplyTarget, FillReport, Job
    from runner import Runner

    with BrowserSession(cfg) as session:
        runner = Runner(
            cfg, db, gemini, session, mode=args.mode, budget=budget, skip_login=True
        )
        page = session.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        filler_cls = ats_detect.detect(args.url, page)
        filler = filler_cls(page)
        console.print("Detected hiring system: [bold]{0}[/]".format(filler.name))

        title = ""
        try:
            title = (page.title() or "").strip()
        except Exception:
            pass

        job = Job(
            site="career_pages",
            title=title or "single URL",
            company="(from --url)",
            url=args.url,
            description=(page.inner_text("body", timeout=8000) or "")[:8000],
            apply_target=ApplyTarget.CAREER_PAGE,
            ats=filler.name,
        )

        if not filler.open_form():
            console.print("[red]No application form found on that page.[/]")
            return 1

        resume = cfg.resumes[0]
        report = FillReport(ats=filler.name)
        ctx = FillContext(
            cfg=cfg, job=job, resume=resume, gemini=gemini, report=report, pacer=runner.pacer
        )
        filler.fill(ctx)

        class _Match:
            match_score = 100
            best_resume = resume.name
            reason = "single-URL mode, scoring skipped"
            red_flags: list[str] = []

        outcome = runner.finish(page, filler, job, _Match(), report, allow_auto=False)
        console.print("Outcome: [bold]{0}[/] {1}".format(outcome.status, outcome.reason))
        if outcome.screenshot:
            console.print("Screenshot: " + outcome.screenshot)
    return 0


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Only the apply loop needs a complete config and a working API key.
    # --check reports problems itself; --report/--export/--login are offline or
    # don't touch Gemini, and should work before the profile is filled in.
    offline = args.check or args.report or args.export or args.login
    cfg = load_config(args.config, strict=not offline)
    log_path = setup_logging(cfg, args.verbose)
    db = open_db(cfg.abs_path(cfg.paths.db))

    try:
        if args.check:
            return cmd_check(cfg, db)
        if args.report:
            return cmd_report(cfg, db, args.report_days)
        if args.export:
            return cmd_export(cfg, db)
        if args.followups:
            return cmd_followups(cfg, db)

        sites = selected_sites(cfg, args.site)
        if args.login:
            return cmd_login(cfg, sites)

        return cmd_run(cfg, db, args, sites, log_path)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
