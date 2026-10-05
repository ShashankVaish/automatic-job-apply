"""Cold email outreach: build the queue, review it, send it, follow up once.

Two modes, per the spec:

  --email-queue (default)  generate emails, save them to ./outbox/ and the
                           database with status "queued", then show each one in
                           the terminal for y / n / edit before sending.
  --email-auto             send everything already queued, without asking.

Both go through `send_queue()`, which refuses to send unless every gate passes:

  * the Mon-Fri 09:30-12:30 window (configurable, enforced by default)
  * the daily cap (25, never above 40)
  * max one first-contact email per person, ever
  * max three people per company, HR first and founder/co-founder a day later
  * never the same address twice in 60 days, except the one follow-up
  * never anyone on inputs/blocklist.txt
  * a Gmail search for a reply from that company's domain, run immediately
    before each send - if they replied, all outreach to that company stops
  * a random 3-8 minute gap between sends

`--email-test-to <address>` redirects every send to one address (your own) so
you can preview the real emails in your own inbox.
"""
from __future__ import annotations

import csv
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ai.gemini_client import GeminiClient
from config import Config, ResumeVariant
from contacts import (
    ROLE_COFOUNDER,
    ROLE_FOUNDER,
    ROLE_HR,
    Contact,
    discover_contacts,
    domain_of,
    load_blocklist,
    load_contacts_csv,
    site_domain,
)
from db import Database, append_csv
from limits import daily_email_cap, email_budget_left, email_window_now

log = logging.getLogger(__name__)


def safe_name(text: str, limit: int = 50) -> str:
    """A filesystem-safe stem for a draft file."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-")
    return (cleaned[:limit] or "unknown").strip("-")

LEDGER_COLUMNS = [
    "date",
    "company",
    "recipient_name",
    "recipient_email",
    "recipient_role",
    "job_role",
    "subject",
    "resume_variant",
    "status",
    "gmail_message_id",
    "sent_at",
    "is_followup",
]

ROLE_LABELS = {
    ROLE_HR: "HR / recruiter",
    ROLE_FOUNDER: "Founder",
    ROLE_COFOUNDER: "Co-founder",
}


@dataclass
class Target:
    """One company worth writing to, with whatever we know about it."""

    company: str = ""
    job_role: str = ""
    job_url: str = ""
    website: str = ""
    description: str = ""
    notes: str = ""
    resume_variant: str = ""
    source: str = "csv"

    def label(self) -> str:
        return "{0}{1}".format(self.company or "?", " - " + self.job_role if self.job_role else "")


@dataclass
class QueueStats:
    targets: int = 0
    contacts_found: int = 0
    queued: int = 0
    skipped: int = 0
    no_email: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class SendStats:
    considered: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    declined: int = 0
    edited: int = 0
    replied_stops: int = 0
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ inputs


def outbox_dir(cfg: Config) -> Path:
    d = cfg.abs_path(cfg.outreach.outbox_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_company_targets(path: Path) -> list[Target]:
    """inputs/companies.csv - companies you want to approach."""
    if not path.is_file():
        return []
    out: list[Target] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            company = lower.get("company", "")
            if not company:
                continue
            out.append(
                Target(
                    company=company,
                    job_role=lower.get("role", ""),
                    website=lower.get("website", ""),
                    notes=lower.get("notes", ""),
                    source="companies_csv",
                )
            )
    log.info("Loaded %d company target(s) from %s", len(out), path)
    return out


def load_url_targets(path: Path) -> list[Target]:
    """inputs/job_urls.txt - careers pages or job posts, one per line."""
    if not path.is_file():
        return []
    out: list[Target] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        out.append(Target(job_url=url, website=url, source="job_url"))
    log.info("Loaded %d URL target(s) from %s", len(out), path)
    return out


def targets_from_matched_jobs(cfg: Config, db: Database) -> list[Target]:
    """Jobs already scored at or above the threshold become outreach targets.

    This is the spec's main source: "for each matched job (match_score >= 70)".
    """
    rows = db.matched_jobs_for_outreach(
        cfg.outreach.match_threshold, cfg.outreach.job_lookback_days
    )
    out: list[Target] = []
    for row in rows:
        out.append(
            Target(
                company=row["company"],
                job_role=row["role"],
                job_url=row["url"],
                website=row["url"],
                description=row["description"] or "",
                resume_variant=row["resume_variant"] or "",
                source="matched_job",
            )
        )
    log.info("%d matched job(s) in the last %d days", len(out), cfg.outreach.job_lookback_days)
    return out


def load_targets(cfg: Config, db: Database, *, include_jobs: bool = True) -> list[Target]:
    """Every target, deduplicated by company, matched jobs first."""
    collected: list[Target] = []
    if include_jobs:
        collected += targets_from_matched_jobs(cfg, db)
    collected += load_company_targets(cfg.abs_path(cfg.outreach.companies_csv))
    collected += load_url_targets(cfg.abs_path(cfg.outreach.job_urls))

    from db import _norm  # noqa: PLC0415

    merged: dict[str, Target] = {}
    for target in collected:
        key = _norm(target.company) or target.job_url
        existing = merged.get(key)
        if existing is None:
            merged[key] = target
            continue
        # Keep the richest version of each company.
        for attribute in ("job_role", "website", "description", "notes", "resume_variant"):
            if not getattr(existing, attribute) and getattr(target, attribute):
                setattr(existing, attribute, getattr(target, attribute))
    return list(merged.values())


# ------------------------------------------------------------- the drafts


def draft_filename(company: str, role: str, followup: bool = False) -> str:
    return "{0}_{1}{2}.txt".format(
        safe_name(company), role, "_followup" if followup else ""
    )


def write_draft_file(
    cfg: Config,
    *,
    company: str,
    role: str,
    to: str,
    recipient_name: str,
    subject: str,
    body: str,
    resume: ResumeVariant,
    job_role: str = "",
    job_url: str = "",
    followup: bool = False,
    status: str = "queued",
) -> Path:
    path = outbox_dir(cfg) / draft_filename(company, role, followup)
    header = "FOLLOW-UP (one only)" if followup else "COLD OUTREACH"
    path.write_text(
        "\n".join(
            [
                "To:      {0}{1}".format(
                    to, "  (" + recipient_name + ")" if recipient_name else ""
                ),
                "Subject: " + subject,
                "",
                "Recipient type: " + ROLE_LABELS.get(role, role),
                "Role:    " + (job_role or "(general enquiry)"),
                "Job:     " + (job_url or "-"),
                "Attach:  " + str(resume.abs_pdf_path),
                "Resume link: " + resume.public_link,
                "Status:  " + status + "  (" + header + ")",
                "Queued:  " + date.today().strftime("%Y-%m-%d"),
                "",
                "-" * 60,
                "",
                body,
                "",
                "-" * 60,
                "Queued, not sent. Review with --email-queue, or send with --email-auto.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def ledger_row(row: Any) -> dict[str, str]:
    return {
        "date": row["date"],
        "company": row["company"],
        "recipient_name": row["recipient_name"] or "",
        "recipient_email": row["recipient_email"],
        "recipient_role": row["recipient_role"],
        "job_role": row["job_role"] or "",
        "subject": row["subject"],
        "resume_variant": row["resume_variant"] or "",
        "status": row["status"],
        "gmail_message_id": row["gmail_message_id"] or "",
        "sent_at": row["sent_at"] or "",
        "is_followup": "yes" if row["is_followup"] else "no",
    }


# ------------------------------------------------------------- queue build


def schedule_for(cfg: Config, role: str) -> str:
    """HR goes today; founder and co-founder a day later, per the spec."""
    if role == ROLE_HR:
        return date.today().strftime("%Y-%m-%d")
    delay = max(0, int(cfg.outreach.founder_delay_days))
    return (date.today() + timedelta(days=delay)).strftime("%Y-%m-%d")


def build_queue(
    cfg: Config,
    db: Database,
    gemini: GeminiClient,
    targets: list[Target],
    *,
    page: Any = None,
    limit: int = 0,
    http_get: Any = None,
) -> QueueStats:
    """Find contacts and write a tailored email for each. Nothing is sent."""
    stats = QueueStats(targets=len(targets))
    verified = load_contacts_csv(cfg.abs_path(cfg.outreach.contacts_csv))
    blocklist = load_blocklist(cfg.abs_path(cfg.outreach.blocklist))
    ledger = outbox_dir(cfg) / "outreach.csv"
    max_contacts = max(1, int(cfg.outreach.max_contacts_per_company))

    for target in targets:
        if limit and stats.queued >= limit:
            stats.notes.append("stopped at the --max of {0}".format(limit))
            break

        try:
            if not target.company:
                stats.skipped += 1
                stats.notes.append("no company name for " + (target.job_url or "?"))
                continue

            replied = db.company_replied(target.company)
            if replied is not None:
                stats.skipped += 1
                stats.notes.append(
                    "{0} already replied on {1} - leaving them alone".format(
                        target.company, replied["date_found"]
                    )
                )
                continue

            already = db.company_email_count(target.company)
            if already >= max_contacts:
                stats.skipped += 1
                stats.notes.append(
                    "{0}: already {1} people contacted".format(target.company, already)
                )
                continue

            found = discover_contacts(
                cfg,
                db,
                company=target.company,
                description=target.description,
                job_url=target.job_url,
                website=target.website,
                page=page,
                csv_contacts=verified,
                blocklist=blocklist,
                http_get=http_get,
                max_contacts=max_contacts,
            )
            stats.contacts_found += len(found.contacts)
            stats.no_email += len(found.misses)
            for role, reason in found.misses:
                stats.notes.append(
                    "{0}: {1} for {2}".format(target.company, reason, role)
                )

            if not found.contacts:
                continue

            resume = pick_resume(cfg, gemini, target, found)

            for contact in found.contacts:
                if db.company_email_count(target.company) >= max_contacts:
                    break
                queued = queue_one(
                    cfg, db, gemini, target, contact, resume, ledger=ledger
                )
                if queued == "queued":
                    stats.queued += 1
                elif queued == "failed":
                    stats.failed += 1
                else:
                    stats.skipped += 1
                    stats.notes.append(
                        "{0}: {1}".format(contact.email, queued)
                    )
                if limit and stats.queued >= limit:
                    break

        except Exception as exc:
            log.exception("Queueing failed for %s", target.label())
            stats.failed += 1
            stats.notes.append(
                "{0}: {1}: {2}".format(target.label(), type(exc).__name__, exc)
            )

    return stats


def pick_resume(
    cfg: Config, gemini: GeminiClient, target: Target, found: Any
) -> ResumeVariant:
    """Use the variant the job was already scored for, else ask Gemini."""
    if target.resume_variant:
        return cfg.resume(target.resume_variant)
    try:
        name = gemini.pick_resume_for_company(
            company=target.company,
            role=target.job_role,
            context=target.description or target.notes,
        )
    except Exception as exc:
        log.warning("Could not pick a resume variant (%s); using the first", exc)
        name = cfg.resumes[0].name
    return cfg.resume(name)


def queue_one(
    cfg: Config,
    db: Database,
    gemini: GeminiClient,
    target: Target,
    contact: Contact,
    resume: ResumeVariant,
    *,
    ledger: Path | None = None,
) -> str:
    """Write and queue one email. Returns "queued", "failed", or a skip reason."""
    address = contact.email.strip().lower()

    if db.has_email_for(address):
        return "already emailed or queued (max one per person)"

    recent = db.emailed_recently(address, cfg.outreach.no_repeat_days)
    if recent is not None:
        return "emailed within the last {0} days".format(cfg.outreach.no_repeat_days)

    if not resume.exists():
        log.error("Resume PDF missing: %s", resume.abs_pdf_path)
        return "failed"

    try:
        draft = gemini.outreach_email(
            recipient_role=contact.role,
            company=target.company,
            job_role=target.job_role,
            contact_name=contact.name,
            job_description=target.description,
            company_context=contact.note or target.notes,
            resume_link=resume.public_link,
            resume_variant=resume.name,
        )
    except Exception as exc:
        log.warning("Could not write the email for %s: %s", contact.label(), exc)
        return "failed"

    email_id = db.queue_email(
        company=target.company,
        recipient_email=address,
        recipient_name=contact.name,
        recipient_role=contact.role,
        job_role=target.job_role,
        job_url=target.job_url,
        subject=draft.subject,
        body=draft.body,
        resume_variant=resume.name,
        scheduled_for=schedule_for(cfg, contact.role),
    )

    path = write_draft_file(
        cfg,
        company=target.company,
        role=contact.role,
        to=address,
        recipient_name=contact.name,
        subject=draft.subject,
        body=draft.body,
        resume=resume,
        job_role=target.job_role,
        job_url=target.job_url,
    )
    db.update_email(email_id, draft_path=str(path))

    if ledger is not None:
        row = db.email_row(email_id)
        if row is not None:
            append_csv(ledger, LEDGER_COLUMNS, ledger_row(row))

    log.info("Queued %s email to %s", contact.role, contact.label())
    return "queued"


# ------------------------------------------------------------- follow-ups


def build_followup_queue(
    cfg: Config, db: Database, gemini: GeminiClient, *, limit: int = 0
) -> QueueStats:
    """Queue the single follow-up for HR emails with no reply after N days."""
    stats = QueueStats()
    if cfg.outreach.max_followups <= 0:
        stats.notes.append("follow-ups are disabled (max_followups: 0)")
        return stats

    due = db.emails_due_followup(cfg.outreach.followup_after_days)
    stats.targets = len(due)
    ledger = outbox_dir(cfg) / "outreach.csv"

    for row in due:
        if limit and stats.queued >= limit:
            break

        replied = db.company_replied(row["company"])
        if replied is not None:
            db.mark_followup_queued(int(row["id"]))
            stats.skipped += 1
            stats.notes.append(row["company"] + " replied; no follow-up")
            continue

        resume = cfg.resume(row["resume_variant"] or "")
        try:
            draft = gemini.outreach_followup(
                company=row["company"],
                job_role=row["job_role"] or "",
                contact_name=row["recipient_name"] or "",
                first_contacted=(row["sent_at"] or row["date"])[:10],
                original_subject=row["subject"],
                resume_link=resume.public_link,
            )
        except Exception as exc:
            log.warning("Could not draft a follow-up for %s: %s", row["company"], exc)
            stats.failed += 1
            continue

        email_id = db.queue_email(
            company=row["company"],
            recipient_email=row["recipient_email"],
            recipient_name=row["recipient_name"] or "",
            recipient_role=row["recipient_role"],
            job_role=row["job_role"] or "",
            job_url=row["job_url"] or "",
            subject=draft.subject,
            body=draft.body,
            resume_variant=resume.name,
            is_followup=True,
            followup_of=int(row["id"]),
        )
        path = write_draft_file(
            cfg,
            company=row["company"],
            role=row["recipient_role"],
            to=row["recipient_email"],
            recipient_name=row["recipient_name"] or "",
            subject=draft.subject,
            body=draft.body,
            resume=resume,
            job_role=row["job_role"] or "",
            job_url=row["job_url"] or "",
            followup=True,
        )
        db.update_email(email_id, draft_path=str(path))
        # Marked straight away so a second follow-up can never be queued.
        db.mark_followup_queued(int(row["id"]))

        queued_row = db.email_row(email_id)
        if queued_row is not None:
            append_csv(ledger, LEDGER_COLUMNS, ledger_row(queued_row))

        stats.queued += 1
        log.info("Queued the follow-up to %s at %s", row["recipient_email"], row["company"])

    return stats


# ----------------------------------------------------------- reply/bounce


def sync_replies_and_bounces(
    cfg: Config, db: Database, *, service: Any = None, within_days: int = 60
) -> tuple[int, int]:
    """Read your mailbox for replies and bounces, and act on both."""
    import gmail_client  # noqa: PLC0415

    replies = 0
    bounces = 0

    try:
        bounced = gmail_client.find_bounced_addresses(
            cfg, within_days=within_days, service=service
        )
    except Exception as exc:
        log.warning("Bounce check failed: %s", exc)
        bounced = {}

    for address, reason in bounced.items():
        # Only act on addresses this tool actually wrote to, so an unrelated
        # bounce in your mailbox can't blacklist someone.
        if db.contact(address) is None and not db.has_email_for(address):
            continue
        db.mark_bounced(address, reason)
        bounces += 1
        log.info("Marked %s as bounced; it will never be used again", address)

    companies: dict[str, str] = {}
    for row in db.email_rows(None):
        if row["status"] not in ("sent", "queued"):
            continue
        domain = domain_of(row["recipient_email"])
        if domain:
            companies.setdefault(row["company"], domain)

    for company, domain in companies.items():
        if db.company_replied(company) is not None:
            continue
        try:
            found = gmail_client.find_reply_from_domain(
                cfg, domain, within_days=within_days, service=service
            )
        except Exception as exc:
            log.warning("Reply check failed for %s: %s", company, exc)
            continue
        if found is not None:
            snippet = (found.get("snippet") or "")[:200]
            db.mark_company_replied(company, domain, snippet)
            replies += 1
            log.info("%s replied - all outreach to them stops", company)

    return replies, bounces


# --------------------------------------------------------------- sending


def preview(row: Any, *, test_to: str = "") -> str:
    """The terminal preview shown before each send."""
    width = 72
    lines = [
        "=" * width,
        "  To:      {0}{1}".format(
            row["recipient_email"],
            "  (" + row["recipient_name"] + ")" if row["recipient_name"] else "",
        ),
    ]
    if test_to:
        lines.append("  TEST MODE: this will actually go to " + test_to)
    lines += [
        "  Type:    " + ROLE_LABELS.get(row["recipient_role"], row["recipient_role"]),
        "  Company: " + row["company"],
        "  Role:    " + (row["job_role"] or "(general enquiry)"),
        "  Resume:  " + (row["resume_variant"] or "-"),
        "  Subject: " + row["subject"],
    ]
    if row["is_followup"]:
        lines.append("  (follow-up - the only one that will be sent)")
    lines += ["-" * width, ""]
    lines += ["  " + line for line in (row["body"] or "").splitlines()]
    lines += ["", "=" * width]
    return "\n".join(lines)


def edit_body(current: str, ask: Any = None) -> str:
    """Let the user retype the body in the terminal. Blank line finishes.

    `ask` is injectable so the review loop can be driven in tests.
    """
    read = ask or input
    print("  Type the new body. Finish with an empty line, or just press Enter")
    print("  twice to keep what's there.")
    collected: list[str] = []
    blanks = 0
    while True:
        try:
            line = read("")
        except EOFError:
            break
        if line.strip() == "":
            blanks += 1
            if blanks >= 1 and collected:
                break
            if blanks >= 2:
                return current
            continue
        blanks = 0
        collected.append(line)
    return "\n".join(collected) if collected else current


def send_queue(
    cfg: Config,
    db: Database,
    *,
    mode: str = "queue",
    service: Any = None,
    test_to: str = "",
    limit: int = 0,
    ignore_window: bool = False,
    sleep: Any = None,
    prompt: Any = None,
) -> SendStats:
    """Send queued emails.

    mode "queue" asks y/n/edit for each one; mode "auto" sends without asking.
    Every gate is checked here, and `confirmed=True` is only ever passed to the
    Gmail client once they all pass.
    """
    import gmail_client  # noqa: PLC0415

    stats = SendStats()
    ask = prompt or input
    pause = sleep if sleep is not None else time.sleep

    if not ignore_window:
        ok, why = email_window_now(cfg)
        if not ok:
            stats.notes.append(why)
            log.info("Not sending: %s", why)
            return stats

    budget, budget_note = email_budget_left(cfg, db)
    if budget <= 0:
        stats.notes.append("daily sending cap reached ({0})".format(budget_note))
        return stats

    blocklist = load_blocklist(cfg.abs_path(cfg.outreach.blocklist))
    rows = db.queued_emails(due_only=True)
    stats.considered = len(rows)
    if not rows:
        stats.notes.append("nothing is queued and due")
        return stats

    ledger = outbox_dir(cfg) / "outreach.csv"
    sent_this_run = 0

    for row in rows:
        if limit and sent_this_run >= limit:
            stats.notes.append("stopped at the --max of {0}".format(limit))
            break
        if sent_this_run >= budget:
            stats.notes.append(
                "daily sending cap reached ({0})".format(daily_email_cap(cfg))
            )
            break
        if not ignore_window:
            ok, why = email_window_now(cfg)
            if not ok:
                stats.notes.append("sending window closed: " + why)
                break

        email_id = int(row["id"])
        address = row["recipient_email"]
        company = row["company"]

        # --- gates -------------------------------------------------------
        from contacts import is_blocked  # noqa: PLC0415

        if is_blocked(address, blocklist):
            db.update_email(email_id, status="skipped", error="on the blocklist")
            stats.skipped += 1
            stats.notes.append(address + " is on the blocklist")
            continue

        contact_row = db.contact(address)
        if contact_row is not None and contact_row["status"] == "bounced":
            db.update_email(email_id, status="skipped", error="address previously bounced")
            stats.skipped += 1
            stats.notes.append(address + " bounced before")
            continue

        if db.company_replied(company) is not None:
            db.update_email(email_id, status="skipped", error="company replied")
            stats.skipped += 1
            stats.replied_stops += 1
            continue

        if not row["is_followup"]:
            recent = db.emailed_recently(address, cfg.outreach.no_repeat_days)
            if recent is not None:
                db.update_email(
                    email_id,
                    status="skipped",
                    error="emailed within {0} days".format(cfg.outreach.no_repeat_days),
                )
                stats.skipped += 1
                continue

        # Check their domain for a reply right before sending.
        domain = domain_of(address)
        try:
            replied = gmail_client.find_reply_from_domain(
                cfg, domain, within_days=cfg.outreach.no_repeat_days, service=service
            )
        except Exception as exc:
            log.warning("Could not check for a reply from %s: %s", domain, exc)
            replied = None
        if replied is not None:
            db.mark_company_replied(company, domain, (replied.get("snippet") or "")[:200])
            stats.skipped += 1
            stats.replied_stops += 1
            stats.notes.append(company + " has already replied - stopping outreach to them")
            continue

        # --- review ------------------------------------------------------
        subject = row["subject"]
        body = row["body"]

        if mode == "queue":
            print(preview(row, test_to=test_to))
            answer = ""
            while answer not in ("y", "n", "e", "q"):
                answer = (ask("  Send this? [y]es / [n]o / [e]dit / [q]uit: ") or "").strip().lower()
                answer = answer[:1] if answer else ""
            if answer == "q":
                stats.notes.append("you quit the review")
                break
            if answer == "n":
                db.update_email(email_id, status="skipped", error="you declined")
                stats.declined += 1
                continue
            if answer == "e":
                new_subject = (ask("  New subject (Enter keeps it): ") or "").strip()
                if new_subject:
                    subject = new_subject
                body = edit_body(body, ask)
                db.update_email(email_id, subject=subject, body=body)
                stats.edited += 1
                print(preview(db.email_row(email_id), test_to=test_to))
                confirm = (ask("  Send the edited version? [y/n]: ") or "").strip().lower()
                if not confirm.startswith("y"):
                    db.update_email(email_id, status="skipped", error="you declined")
                    stats.declined += 1
                    continue

        # --- send --------------------------------------------------------
        resume = cfg.resume(row["resume_variant"] or "")
        recipient = test_to or address
        thread_id = ""
        thread_message_id = ""
        if row["is_followup"] and row["followup_of"]:
            original = db.email_row(int(row["followup_of"]))
            if original is not None:
                thread_id = original["gmail_thread_id"] or ""
                thread_message_id = original["gmail_message_id"] or ""

        try:
            result = gmail_client.send_message(
                cfg,
                to=recipient,
                subject=subject,
                body=body,
                attachment=resume.abs_pdf_path if resume.exists() else None,
                thread_id=thread_id,
                thread_message_id=thread_message_id,
                service=service,
                confirmed=True,
            )
        except Exception as exc:
            log.exception("Send failed for %s", address)
            db.update_email(
                email_id,
                status="failed",
                error="{0}: {1}".format(type(exc).__name__, exc),
            )
            stats.failed += 1
            continue

        db.mark_sent(email_id, message_id=result.message_id, thread_id=result.thread_id)
        sent_row = db.email_row(email_id)
        if sent_row is not None:
            append_csv(ledger, LEDGER_COLUMNS, ledger_row(sent_row))
        stats.sent += 1
        sent_this_run += 1
        log.info(
            "Sent %s email to %s at %s%s",
            row["recipient_role"],
            recipient,
            company,
            " (test mode)" if test_to else "",
        )

        # Human pacing between sends.
        remaining = [r for r in rows if int(r["id"]) > email_id]
        if remaining and sent_this_run < budget:
            gap = random.uniform(
                cfg.outreach.sending.gap_min_s, cfg.outreach.sending.gap_max_s
            )
            log.info("Waiting %.0fs before the next email", gap)
            pause(gap)

    return stats
