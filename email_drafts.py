"""Email applications and follow-ups. Nothing here ever sends anything.

Drafts are written to ./drafts/<company>_<role>.txt as plain text you can read,
edit and send yourself. If config.yaml sets email.gmail_api: true, the draft is
*also* created in your Gmail Drafts folder - still unsent.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from ai.gemini_client import GeminiClient
from config import Config, ResumeVariant
from db import Database, append_csv
from models import Job

log = logging.getLogger(__name__)

FOLLOWUP_COLUMNS = [
    "date_added",
    "applied_on",
    "site",
    "company",
    "role",
    "url",
    "channel",
    "message",
    "sent",
]


def safe_name(text: str, limit: int = 50) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-")
    return (s[:limit] or "unknown").strip("-")


def draft_path(cfg: Config, company: str, role: str) -> Path:
    directory = cfg.abs_path(cfg.email.drafts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "{0}_{1}.txt".format(safe_name(company), safe_name(role))


def draft_application_email(
    cfg: Config, gemini: GeminiClient, job: Job, resume: ResumeVariant
) -> Path:
    """Write an email application draft to disk. Never sends."""
    draft = gemini.email_draft(
        title=job.title,
        company=job.company,
        description=job.description,
        resume_link=resume.public_link,
        to_email=job.apply_email,
    )

    path = draft_path(cfg, job.company, job.title)
    body = "\n".join(
        [
            "To:      " + job.apply_email,
            "Subject: " + draft.subject,
            "",
            "Attach:  " + str(resume.abs_pdf_path),
            "Resume link: " + resume.public_link,
            "Job posting: " + job.apply_url(),
            "Drafted: " + date.today().strftime("%Y-%m-%d"),
            "",
            "-" * 60,
            "",
            draft.body,
            "",
            "-" * 60,
            "NOT SENT. Read it, edit it, then send it yourself.",
            "",
        ]
    )
    path.write_text(body, encoding="utf-8")
    log.info("Email application draft written to %s", path)

    if cfg.email.gmail_api:
        try:
            from gmail_client import create_draft  # noqa: PLC0415

            create_draft(
                cfg,
                to=job.apply_email,
                subject=draft.subject,
                body=draft.body,
                attachment=resume.abs_pdf_path,
            )
            log.info("Gmail draft created for %s", job.label())
        except Exception as exc:
            log.warning("Could not create the Gmail draft: %s: %s", type(exc).__name__, exc)

    return path


# ------------------------------------------------------------------ follow-ups


def build_followups(cfg: Config, db: Database, gemini: GeminiClient) -> list[dict[str, str]]:
    """Add a follow-up row for every application older than followups.days_after.

    Writes to followups.csv. Never sends anything.
    """
    due = db.due_followups(cfg.followups.days_after)
    if not due:
        log.info("No applications are due a follow-up")
        return []

    written: list[dict[str, str]] = []
    for row in due:
        channel = "linkedin" if row["site"] == "linkedin" else "email"
        try:
            message = gemini.follow_up(
                title=row["role"],
                company=row["company"],
                applied_on=row["date"],
                channel=channel,
            )
        except Exception as exc:
            log.warning(
                "Could not draft a follow-up for %s - %s: %s",
                row["company"],
                row["role"],
                exc,
            )
            continue

        entry = {
            "date_added": date.today().strftime("%Y-%m-%d"),
            "applied_on": row["date"],
            "site": row["site"],
            "company": row["company"],
            "role": row["role"],
            "url": row["url"],
            "channel": channel,
            "message": message.replace("\r\n", "\n"),
            "sent": "no",
        }
        append_csv(cfg.abs_path(cfg.followups.csv_path), FOLLOWUP_COLUMNS, entry)
        db.mark_followup_added(int(row["id"]))
        written.append(entry)
        log.info("Follow-up queued for %s - %s", row["company"], row["role"])

    return written
