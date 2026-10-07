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

from rich import box
from rich.console import Console
from rich.markup import escape
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

    # Playwright is extremely chatty at DEBUG, and the Gemini SDK logs an
    # automatic-function-calling notice on every single call.
    logging.getLogger("playwright").setLevel(logging.WARNING)
    for noisy in ("google_genai", "google_genai.models", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("google_genai.models").setLevel(logging.ERROR)
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

    out = p.add_argument_group("cold email outreach (HR, founder, co-founder)")
    out.add_argument(
        "--email-queue",
        action="store_true",
        help="DEFAULT for outreach. Find contacts, write the emails, queue them, "
        "then show each one for y/n/edit before sending.",
    )
    out.add_argument(
        "--email-auto",
        action="store_true",
        help="Send everything already queued, without asking.",
    )
    out.add_argument(
        "--email-followups",
        action="store_true",
        help="Queue the single follow-up for HR emails with no reply",
    )
    out.add_argument(
        "--email-report",
        action="store_true",
        help="Sent / replied / bounced and reply rate for the last 30 days",
    )
    out.add_argument(
        "--email-test-to",
        default="",
        metavar="ADDRESS",
        help="Send every queued email to this address instead of the real "
        "recipient, so you can preview them in your own inbox.",
    )
    out.add_argument(
        "--email-sync",
        action="store_true",
        help="Check your mailbox for replies and bounces, and act on them",
    )
    out.add_argument(
        "--gmail-auth",
        action="store_true",
        help="Do the one-time Gmail browser consent, then exit",
    )
    out.add_argument(
        "--contacts-report",
        action="store_true",
        help="Show every contact found, and where it came from",
    )
    out.add_argument(
        "--no-discover",
        action="store_true",
        help="With --email-queue, only use inputs/contacts.csv - never open a browser",
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
    table = Table(title="Setup check", show_lines=False, box=box.ASCII)
    table.add_column("Item")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    ok = True

    def row(item: str, good: bool, detail: str) -> None:
        nonlocal ok
        if not good:
            ok = False
        # escape() because details carry real data - Windows paths and
        # "[placeholder]" values that rich would otherwise read as markup.
        table.add_row(item, "[green]OK[/]" if good else "[red]FAIL[/]", escape(detail))

    from config import _unfilled

    placeholders = _unfilled(cfg)
    row(
        "config.yaml",
        not placeholders,
        "loaded for " + cfg.profile.name
        if not placeholders
        else "{0} field(s) still have [placeholder] values: {1}".format(
            len(placeholders), ", ".join(placeholders[:6])
        ),
    )
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

    # Both safety windows are defined in IST, so a missing timezone database
    # would silently turn them into local time.
    from limits import resolve_timezone

    zone = cfg.limits.run_window.timezone
    tz, tz_warning = resolve_timezone(zone)
    if tz is None:
        row("timezone", False, tz_warning)
    else:
        from datetime import datetime as _dt

        row(
            "timezone",
            True,
            "{0} is {1}".format(zone, _dt.now(tz).strftime("%a %H:%M")),
        )

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

    summary = Table(title="Last {0} days by source".format(days), box=box.ASCII)
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

    # Six columns, not eight: on an 80-column terminal a wider table makes
    # rich shrink "company" to "compa/ny". Company and role share one
    # flexible column that folds instead of being truncated.
    detail = Table(title="Activity detail", show_lines=False, box=box.ASCII)
    detail.add_column("date", no_wrap=True, min_width=10)
    detail.add_column("site", no_wrap=True, min_width=6)
    detail.add_column("company - role", overflow="fold", min_width=16)
    detail.add_column("score / resume", no_wrap=True)
    detail.add_column("status", no_wrap=True, min_width=12)
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
            "{0} - {1}".format(clip(r["company"], 28), clip(r["role"], 34)),
            "{0:>3} {1}".format(
                r["match_score"] if r["match_score"] is not None else "-",
                r["resume_variant"] or "",
            ).rstrip(),
            "[{0}]{1}[/]".format(colour, status),
        )
    console.print(detail)
    if len(rows) > 60:
        console.print("[dim]... and {0} more rows. See applications.csv[/]".format(len(rows) - 60))

    skipped_reasons = [r for r in rows if r["status"] == "skipped" and r["error"]]
    if skipped_reasons:
        reasons = Table(title="Why jobs were skipped", box=box.ASCII)
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


# -------------------------------------------------------------- outreach


def _gemini(cfg: Config):
    from ai.gemini_client import GeminiClient

    return GeminiClient(cfg)


def _gmail_service(cfg: Config):
    """Build the Gmail service up front, or explain what to do instead.

    Returns (service, None) on success, or (None, exit_code) when the caller
    should stop. Resolving it here means a missing credentials.json is a clear
    message before anything is shown, rather than an exception part-way through
    a review you already spent time on.
    """
    import gmail_client

    try:
        return gmail_client.get_service(cfg, interactive=False), None
    except gmail_client.GmailNotConfigured as exc:
        first_line = str(exc).strip().splitlines()[0]
        console.print("[red]Gmail is not ready.[/] " + escape(first_line))
        console.print(
            "Run [bold]python main.py --gmail-auth[/] once to authorise it, "
            "then try again. Section 11 of README.md has the Google Cloud setup."
        )
        return None, 1
    except Exception as exc:
        console.print(
            "[red]Could not reach Gmail:[/] {0}: {1}".format(
                type(exc).__name__, escape(str(exc))
            )
        )
        return None, 1


def cmd_gmail_auth(cfg: Config) -> int:
    """Do the one-time Gmail consent so later runs are silent."""
    import gmail_client

    try:
        service = gmail_client.get_service(cfg, interactive=True)
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        console.print("[red]Gmail authorisation failed:[/] " + escape(str(exc)))
        return 1
    console.print(
        "[green]Gmail authorised[/] for "
        + escape(str(profile.get("emailAddress", "your account")))
    )
    console.print("Token cached - you won't be asked again unless you revoke access.")
    return 0


def cmd_email_queue(cfg: Config, db: Database, args) -> int:
    """Find contacts, write the emails, queue them, then review and send."""
    from browser.launcher import BrowserSession
    from outreach import build_queue, load_targets

    if not cfg.outreach.enabled:
        console.print("[yellow]Outreach is disabled[/] (outreach.enabled in config.yaml)")
        return 0

    targets = load_targets(cfg, db)
    if not targets:
        console.print(
            "[yellow]No outreach targets.[/] Either run the job search first so "
            "there are matched jobs, or fill in [bold]inputs/companies.csv[/] "
            "(copy it from companies.example.csv)."
        )
        return 0

    console.print(
        "Building the outreach queue for {0} company target(s). "
        "Nothing sends until you approve it.".format(len(targets))
    )

    gemini = _gemini(cfg)
    needs_browser = not args.no_discover and any(t.website for t in targets)

    if needs_browser:
        with BrowserSession(cfg) as session:
            stats = build_queue(
                cfg, db, gemini, targets, page=session.new_page(), limit=args.max
            )
    else:
        stats = build_queue(cfg, db, gemini, targets, limit=args.max)

    table = Table(title="Outreach queue built", box=box.ASCII)
    for col in ("targets", "contacts", "queued", "no address", "skipped", "failed"):
        table.add_column(col, justify="right", no_wrap=True)
    table.add_row(
        str(stats.targets),
        str(stats.contacts_found),
        str(stats.queued),
        str(stats.no_email),
        str(stats.skipped),
        str(stats.failed),
    )
    console.print(table)
    for note in stats.notes[:20]:
        console.print("[yellow]note:[/] " + escape(note))

    queued = db.queued_emails(due_only=True)
    if not queued:
        waiting = db.queued_emails(due_only=False)
        if waiting:
            console.print(
                "Nothing is due to send today. {0} email(s) are waiting - founder "
                "and co-founder emails are scheduled a day after the HR one, so "
                "run this again tomorrow.".format(len(waiting))
            )
        elif stats.failed:
            console.print(
                "[red]Nothing could be queued.[/] {0} email(s) failed to be "
                "written - check the log at the path above.".format(stats.failed)
            )
        else:
            console.print("Nothing new to queue.")
        return 0

    console.print(
        "\n[bold]{0}[/] email(s) queued and due. Reviewing them now - "
        "y to send, n to skip, e to edit, q to stop.".format(len(queued))
    )
    return cmd_email_send(cfg, db, args, mode="queue")


def cmd_email_send(cfg: Config, db: Database, args, *, mode: str) -> int:
    """Send the queued emails, asking first unless mode is "auto"."""
    from limits import email_window_now
    from outreach import send_queue

    test_to = (args.email_test_to or "").strip()
    if test_to:
        console.print(
            "[cyan]TEST MODE[/] - every email goes to [bold]{0}[/] instead of the "
            "real recipient.".format(escape(test_to))
        )

    if mode == "auto" and not test_to:
        console.print(
            "[yellow]AUTO SEND[/] - queued emails will be sent without asking, "
            "inside the sending window and the daily cap."
        )

    ok, why = email_window_now(cfg)
    if not ok and not args.ignore_run_window:
        console.print("[red]Not sending.[/] " + escape(why))
        console.print(
            "Queued emails stay queued. Run again inside the window, or pass "
            "--ignore-run-window."
        )
        return 2

    # Resolved before the first email is shown, so an authorisation problem
    # never interrupts a review half way through.
    service, failure = _gmail_service(cfg)
    if service is None:
        return failure or 1

    stats = send_queue(
        cfg,
        db,
        mode=mode,
        service=service,
        test_to=test_to,
        limit=args.max,
        ignore_window=args.ignore_run_window,
    )

    table = Table(title="Sending", box=box.ASCII)
    for col in ("considered", "sent", "declined", "edited", "skipped", "failed"):
        table.add_column(col, justify="right", no_wrap=True)
    table.add_row(
        str(stats.considered),
        str(stats.sent),
        str(stats.declined),
        str(stats.edited),
        str(stats.skipped),
        str(stats.failed),
    )
    console.print(table)
    for note in stats.notes[:20]:
        console.print("[yellow]note:[/] " + escape(note))
    if stats.replied_stops:
        console.print(
            "[green]{0} company/companies had already replied[/] - outreach to "
            "them has stopped.".format(stats.replied_stops)
        )
    return 0


def cmd_email_followups(cfg: Config, db: Database, args) -> int:
    """Queue the one permitted follow-up, then review and send it."""
    from outreach import build_followup_queue

    stats = build_followup_queue(cfg, db, _gemini(cfg), limit=args.max)
    if not stats.queued:
        console.print(
            "[yellow]Nothing is due a follow-up[/] (due {0} days after the first "
            "email, HR contacts only, one follow-up ever).".format(
                cfg.outreach.followup_after_days
            )
        )
        for note in stats.notes[:10]:
            console.print("[yellow]note:[/] " + escape(note))
        return 0

    console.print(
        "[bold]{0}[/] follow-up(s) queued out of {1} due.".format(
            stats.queued, stats.targets
        )
    )
    mode = "auto" if args.email_auto else "queue"
    return cmd_email_send(cfg, db, args, mode=mode)


def cmd_email_sync(cfg: Config, db: Database) -> int:
    """Read your mailbox for replies and bounces."""
    from outreach import sync_replies_and_bounces

    service, failure = _gmail_service(cfg)
    if service is None:
        return failure or 1

    replies, bounces = sync_replies_and_bounces(cfg, db, service=service)
    console.print(
        "Found [bold]{0}[/] new repl{1} and [bold]{2}[/] bounce{3}.".format(
            replies, "y" if replies == 1 else "ies", bounces, "" if bounces == 1 else "s"
        )
    )
    if replies:
        console.print("Outreach to companies that replied has stopped.")
    if bounces:
        console.print("Bounced addresses will never be used again.")
    return 0


def cmd_contacts_report(cfg: Config, db: Database) -> int:
    rows = db.all_contacts()
    if not rows:
        console.print(
            "[yellow]No contacts found yet.[/] Add your own to "
            "[bold]inputs/contacts.csv[/], or run --email-queue to discover some."
        )
        return 0

    table = Table(title="Contacts", box=box.ASCII)
    table.add_column("company", overflow="fold", min_width=14)
    table.add_column("role", no_wrap=True)
    table.add_column("name", overflow="fold")
    table.add_column("email", overflow="fold")
    table.add_column("source", no_wrap=True)
    table.add_column("conf", justify="right", no_wrap=True)
    table.add_column("status", no_wrap=True)
    for r in rows:
        colour = {"active": "white", "bounced": "red", "blocked": "dim"}.get(
            r["status"], "white"
        )
        table.add_row(
            clip(r["company"], 24),
            r["role"],
            clip(r["name"] or "-", 18),
            clip(r["email"], 30),
            r["source"] or "-",
            str(r["confidence"] or "-") + ("v" if r["verified"] else ""),
            "[{0}]{1}[/]".format(colour, r["status"]),
        )
    console.print(table)
    console.print(
        "[dim]conf = confidence; 'v' means you supplied it in inputs/contacts.csv.[/]"
    )

    misses = db.conn.execute(
        "SELECT * FROM contact_misses ORDER BY date_tried DESC LIMIT 20"
    ).fetchall()
    if misses:
        miss_table = Table(title="No work email found", box=box.ASCII)
        miss_table.add_column("company", overflow="fold")
        miss_table.add_column("role", no_wrap=True)
        miss_table.add_column("tried", no_wrap=True)
        for m in misses:
            miss_table.add_row(clip(m["company"], 30), m["role"], m["date_tried"])
        console.print(miss_table)
        console.print(
            "[dim]Addresses are never guessed, so these need you to find the "
            "right person.[/]"
        )
    return 0


def cmd_email_report(cfg: Config, db: Database, days: int = 30) -> int:
    stats = db.email_stats(days)
    rows = db.email_rows(days)

    if not rows:
        console.print("[yellow]No outreach emails in the last {0} days.[/]".format(days))
        return 0

    summary = Table(title="Outreach, last {0} days".format(days), box=box.ASCII)
    for col in ("queued", "sent", "replied", "bounced", "failed", "skipped", "reply rate"):
        summary.add_column(col, justify="right", no_wrap=True)
    summary.add_row(
        str(stats["queued"]),
        str(stats["sent"]),
        str(stats["replied"]),
        str(stats["bounced"]),
        str(stats["failed"]),
        str(stats["skipped"]),
        "{0}%".format(stats["reply_rate"]),
    )
    console.print(summary)

    detail = Table(title="Emails", box=box.ASCII)
    detail.add_column("date", no_wrap=True, min_width=10)
    detail.add_column("company - recipient", overflow="fold", min_width=18)
    detail.add_column("type", no_wrap=True)
    detail.add_column("status", no_wrap=True)
    for r in rows[:60]:
        colour = {
            "sent": "cyan",
            "replied": "green",
            "bounced": "red",
            "failed": "red",
            "queued": "yellow",
            "skipped": "dim",
        }.get(r["status"], "white")
        label = r["recipient_role"]
        if r["is_followup"]:
            label += " f/up"
        detail.add_row(
            r["date"],
            "{0} - {1}".format(clip(r["company"], 22), clip(r["recipient_email"], 28)),
            label,
            "[{0}]{1}[/]".format(colour, r["status"]),
        )
    console.print(detail)

    if stats["replied"]:
        console.print(
            "[green]{0} compan{1} replied.[/] Outreach to them has stopped - go "
            "and answer them.".format(
                stats["replied"], "y" if stats["replied"] == 1 else "ies"
            )
        )
    console.print(
        "Reply rate counts replies against delivered mail (sent + replied)."
    )
    return 0


# ---------------------------------------------------------------- the run


def print_run_summary(cfg: Config, db: Database, stats) -> None:
    table = Table(title="This run", box=box.ASCII)
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
    offline = (
        args.check
        or args.report
        or args.export
        or args.login
        or args.email_report
        or args.contacts_report
        or args.gmail_auth
    )
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
        if args.email_report:
            return cmd_email_report(cfg, db, 30)
        if args.contacts_report:
            return cmd_contacts_report(cfg, db)
        if args.gmail_auth:
            return cmd_gmail_auth(cfg)
        if args.email_sync:
            return cmd_email_sync(cfg, db)
        if args.email_followups:
            return cmd_email_followups(cfg, db, args)
        if args.email_auto:
            return cmd_email_send(cfg, db, args, mode="auto")
        if args.email_queue:
            return cmd_email_queue(cfg, db, args)

        sites = selected_sites(cfg, args.site)
        if args.login:
            return cmd_login(cfg, sites)

        return cmd_run(cfg, db, args, sites, log_path)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
