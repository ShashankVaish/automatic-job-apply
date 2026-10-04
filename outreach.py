"""Cold email outreach: drafting introductions to companies that aren't hiring
publicly, plus the follow-up ladder.

This replaces sections 8 (email applications) and 10 (follow-ups) of the
original spec. The core rule is unchanged and absolute: **nothing is ever
sent.** Every message lands in ./outbox/ as a text file, optionally also as a
Gmail draft, and you send it yourself.

Two kinds of input, both under ./inputs/:

  companies.csv   explicit targets: company,email,contact_name,role,website,notes
  job_urls.txt    one URL per line - a careers page or job post. The page is
                  read for the company name, the role, and any contact address
                  published on it.

Addresses are never guessed. We only use an address you supplied or one the
company published on its own page. Inventing firstname@company.com would be
spam and would burn your real name, so it is not done.
"""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from ai.gemini_client import GeminiClient
from config import Config, ResumeVariant
from db import Database, append_csv
from email_drafts import safe_name

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")

# Addresses that are never a person and never worth cold emailing.
JUNK_LOCAL_PARTS = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "postmaster",
    "abuse",
    "unsubscribe",
    "bounce",
    "mailer-daemon",
    "privacy",
    "legal",
    "dmca",
    "security",
    "sales",
    "billing",
    "invoice",
    "support",
)

# Addresses that are plausibly a hiring inbox, best first.
PREFERRED_LOCAL_PARTS = (
    "careers",
    "jobs",
    "hiring",
    "recruit",
    "talent",
    "hr",
    "people",
    "internships",
    "work",
    "team",
    "hello",
    "contact",
    "info",
)

CSV_INPUT_COLUMNS = ("company", "email", "contact_name", "role", "website", "notes")

LEDGER_COLUMNS = [
    "date",
    "company",
    "role",
    "contact_name",
    "email",
    "source",
    "source_url",
    "resume_variant",
    "subject",
    "draft_path",
    "status",
    "sent",
]

FOLLOWUP_LEDGER_COLUMNS = [
    "date_added",
    "first_contacted",
    "stage",
    "company",
    "role",
    "contact_name",
    "email",
    "subject",
    "message",
    "draft_path",
    "sent",
]


@dataclass
class Target:
    """One company we might write to."""

    company: str = ""
    email: str = ""
    contact_name: str = ""
    role: str = ""
    website: str = ""
    notes: str = ""
    source: str = "csv"
    source_url: str = ""
    context: str = ""

    def label(self) -> str:
        return "{0} <{1}>".format(self.company or "?", self.email or "no address")


@dataclass
class OutreachStats:
    found: int = 0
    drafted: int = 0
    skipped: int = 0
    needs_email: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- addresses


def classify_address(email: str) -> int:
    """Rank an address: lower is better, -1 means never use it."""
    local = (email or "").split("@")[0].lower()
    if not local:
        return -1
    for junk in JUNK_LOCAL_PARTS:
        if junk in local:
            return -1
    for i, good in enumerate(PREFERRED_LOCAL_PARTS):
        if good in local:
            return i
    return len(PREFERRED_LOCAL_PARTS)


def best_address(candidates: Iterable[str]) -> str:
    """Pick the most plausible hiring address, or "" if none is usable."""
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in candidates:
        email = (raw or "").strip().strip(".,;:()<>[]").lower()
        if not email or email in seen:
            continue
        seen.add(email)
        rank = classify_address(email)
        if rank < 0:
            continue
        ranked.append((rank, email))
    if not ranked:
        return ""
    ranked.sort(key=lambda pair: (pair[0], len(pair[1])))
    return ranked[0][1]


def addresses_in(text: str) -> list[str]:
    return EMAIL_RE.findall(text or "")


# ------------------------------------------------------------------ inputs


def load_csv_targets(path: Path) -> list[Target]:
    if not path.is_file():
        return []
    targets: list[Target] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            company = lower.get("company", "")
            email = lower.get("email", "")
            if not company and not email:
                continue
            targets.append(
                Target(
                    company=company,
                    email=email.lower(),
                    contact_name=lower.get("contact_name", ""),
                    role=lower.get("role", ""),
                    website=lower.get("website", ""),
                    notes=lower.get("notes", ""),
                    source="csv",
                    source_url=lower.get("website", ""),
                )
            )
    log.info("Loaded %d target(s) from %s", len(targets), path)
    return targets


def load_url_targets(path: Path) -> list[Target]:
    if not path.is_file():
        return []
    targets: list[Target] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        targets.append(Target(source="job_url", source_url=url, website=url))
    log.info("Loaded %d URL target(s) from %s", len(targets), path)
    return targets


def load_targets(cfg: Config, *, csv_path: str = "", urls_path: str = "") -> list[Target]:
    inputs = cfg.abs_path("./inputs")
    csv_file = cfg.abs_path(csv_path) if csv_path else inputs / "companies.csv"
    urls_file = cfg.abs_path(urls_path) if urls_path else inputs / "job_urls.txt"
    return load_csv_targets(csv_file) + load_url_targets(urls_file)


# --------------------------------------------------------- page enrichment

CONTACT_PAGE_HINTS = ("contact", "careers", "jobs", "about", "team", "work-with-us")

COMPANY_NAME_SELECTORS = [
    "meta[property='og:site_name']",
    "meta[name='application-name']",
]

ROLE_SELECTORS = ["h1", "h2.job-title", "[class*='job-title']", "title"]


def enrich_from_page(page: Any, target: Target, *, follow_contact_page: bool = True) -> Target:
    """Read a careers page for the company name, role and a published address."""
    try:
        page.goto(target.source_url, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("Could not open %s: %s", target.source_url, exc)
        return target

    if not target.company:
        for sel in COMPANY_NAME_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    value = (loc.get_attribute("content", timeout=2000) or "").strip()
                    if value:
                        target.company = value
                        break
            except Exception:
                continue
    if not target.company:
        try:
            title = (page.title() or "").strip()
            # "Careers at Fixture Labs" / "Fixture Labs | Jobs"
            parts = re.split(r"\s[\|\-–—]\s|\sat\s", title)
            target.company = (parts[-1] if len(parts) > 1 else title).strip()
        except Exception:
            pass

    if not target.role:
        for sel in ROLE_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    value = " ".join((loc.inner_text(timeout=2500) or "").split())
                    if value:
                        target.role = value[:120]
                        break
            except Exception:
                continue

    body = ""
    try:
        body = page.inner_text("body", timeout=8000) or ""
    except Exception:
        body = ""
    target.context = " ".join(body.split())[:4000]

    found = addresses_in(body)
    try:
        html = page.content()
        found += [
            m.group(1)
            for m in re.finditer(r"mailto:([^\"'?>\s]+)", html, flags=re.I)
        ]
    except Exception:
        pass

    if not target.email:
        target.email = best_address(found)

    # Many sites keep the address on a separate contact page.
    if not target.email and follow_contact_page:
        for hint in CONTACT_PAGE_HINTS:
            try:
                link = page.locator("a[href*='{0}']".format(hint)).first
                if not link.count():
                    continue
                href = (link.get_attribute("href", timeout=1500) or "").strip()
                if not href:
                    continue
                page.goto(href, wait_until="domcontentloaded")
                sub = page.inner_text("body", timeout=6000) or ""
                target.email = best_address(addresses_in(sub))
                if target.email:
                    log.info("Found %s on the %s page", target.email, hint)
                    break
            except Exception:
                continue

    return target


# -------------------------------------------------------------- the drafts


def outbox_dir(cfg: Config) -> Path:
    d = cfg.abs_path(cfg.outreach.outbox_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def draft_filename(company: str, stage: int = 0) -> str:
    suffix = "" if stage == 0 else "_followup{0}".format(stage)
    return "{0}{1}.txt".format(safe_name(company), suffix)


def write_draft_file(
    cfg: Config,
    *,
    company: str,
    to: str,
    subject: str,
    body: str,
    resume: ResumeVariant,
    source_url: str = "",
    stage: int = 0,
) -> Path:
    path = outbox_dir(cfg) / draft_filename(company, stage)
    header = "COLD OUTREACH" if stage == 0 else "FOLLOW-UP {0}".format(stage)
    path.write_text(
        "\n".join(
            [
                "To:      " + to,
                "Subject: " + subject,
                "",
                "Attach:  " + str(resume.abs_pdf_path),
                "Resume link: " + resume.public_link,
                "Source: " + (source_url or "(from inputs/companies.csv)"),
                "Drafted: " + date.today().strftime("%Y-%m-%d") + "  (" + header + ")",
                "",
                "-" * 60,
                "",
                body,
                "",
                "-" * 60,
                "NOT SENT. Read it, edit it, then send it yourself.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def maybe_gmail_draft(
    cfg: Config, *, to: str, subject: str, body: str, resume: ResumeVariant
) -> str:
    """Create a Gmail draft if enabled. Never sends. Returns the draft id."""
    if not cfg.email.gmail_api:
        return ""
    try:
        from gmail_client import create_draft  # noqa: PLC0415

        return create_draft(
            cfg, to=to, subject=subject, body=body, attachment=resume.abs_pdf_path
        )
    except Exception as exc:
        log.warning("Could not create the Gmail draft: %s: %s", type(exc).__name__, exc)
        return ""


def draft_outreach(
    cfg: Config,
    db: Database,
    gemini: GeminiClient,
    targets: list[Target],
    *,
    page: Any | None = None,
    limit: int = 0,
    dedupe_days: int = 90,
) -> OutreachStats:
    """Draft one cold email per target. Never sends anything."""
    stats = OutreachStats(found=len(targets))
    ledger = outbox_dir(cfg) / "outreach.csv"
    daily_cap = cfg.limits.daily_cap("outreach")
    drafted_now = 0

    for target in targets:
        if limit and drafted_now >= limit:
            stats.notes.append("stopped at --max {0}".format(limit))
            break
        if db.count_outreach_today() + drafted_now >= daily_cap:
            stats.notes.append("outreach daily limit reached ({0})".format(daily_cap))
            break

        try:
            if target.source == "job_url" and page is not None:
                target = enrich_from_page(page, target)

            if not target.company:
                stats.skipped += 1
                stats.notes.append("no company name for " + (target.source_url or "?"))
                continue

            if not target.email:
                log.info("No published address for %s - skipping", target.company)
                db.record_outreach(
                    company=target.company,
                    email="unknown@" + safe_name(target.company).lower(),
                    role=target.role,
                    source=target.source,
                    source_url=target.source_url,
                    status="needs_email",
                    note="no address published; addresses are never guessed",
                )
                stats.needs_email += 1
                continue

            seen = db.outreach_seen(target.email)
            if seen is not None and seen["status"] == "drafted":
                stats.skipped += 1
                stats.notes.append(
                    "already drafted to {0} on {1}".format(target.email, seen["date"])
                )
                continue

            dup = (
                db.outreach_company_seen(target.company, dedupe_days)
                if dedupe_days > 0
                else None
            )
            if dup is not None:
                stats.skipped += 1
                stats.notes.append(
                    "already contacted {0} on {1}".format(target.company, dup["date"])
                )
                continue

            variant_name = gemini.pick_resume_for_company(
                company=target.company, role=target.role, context=target.context
            )
            resume = cfg.resume(variant_name)
            if not resume.exists():
                stats.failed += 1
                stats.notes.append("resume PDF missing: " + str(resume.abs_pdf_path))
                continue

            draft = gemini.outreach_email(
                company=target.company,
                role=target.role,
                contact_name=target.contact_name,
                company_context=target.context or target.notes,
                resume_link=resume.public_link,
            )

            path = write_draft_file(
                cfg,
                company=target.company,
                to=target.email,
                subject=draft.subject,
                body=draft.body,
                resume=resume,
                source_url=target.source_url,
            )
            maybe_gmail_draft(
                cfg, to=target.email, subject=draft.subject, body=draft.body, resume=resume
            )

            db.record_outreach(
                company=target.company,
                email=target.email,
                role=target.role,
                contact_name=target.contact_name,
                source=target.source,
                source_url=target.source_url,
                resume_variant=resume.name,
                status="drafted",
                draft_path=str(path),
            )
            append_csv(
                ledger,
                LEDGER_COLUMNS,
                {
                    "date": date.today().strftime("%Y-%m-%d"),
                    "company": target.company,
                    "role": target.role,
                    "contact_name": target.contact_name,
                    "email": target.email,
                    "source": target.source,
                    "source_url": target.source_url,
                    "resume_variant": resume.name,
                    "subject": draft.subject,
                    "draft_path": str(path),
                    "status": "drafted",
                    "sent": "no",
                },
            )
            stats.drafted += 1
            drafted_now += 1
            log.info("Drafted outreach to %s", target.label())

        except Exception as exc:
            log.exception("Outreach failed for %s", target.label())
            stats.failed += 1
            stats.notes.append(
                "{0}: {1}: {2}".format(target.label(), type(exc).__name__, exc)
            )

    return stats


# ------------------------------------------------------------- follow-ups


def draft_outreach_followups(
    cfg: Config, db: Database, gemini: GeminiClient, *, max_stage: int = 2
) -> list[dict[str, str]]:
    """Draft follow-up 1 then 2 for cold emails with no reply. Never sends."""
    due = db.outreach_due_followup(cfg.followups.days_after, max_stage=max_stage)
    if not due:
        log.info("No outreach is due a follow-up")
        return []

    ledger = outbox_dir(cfg) / "outreach_followups.csv"
    written: list[dict[str, str]] = []

    for row in due:
        stage = int(row["followup_stage"]) + 1
        resume = cfg.resume(row["resume_variant"] or "")
        try:
            draft = gemini.outreach_followup(
                company=row["company"],
                role=row["role"] or "",
                contact_name=row["contact_name"] or "",
                first_contacted=row["date"],
                stage=stage,
                resume_link=resume.public_link,
            )
        except Exception as exc:
            log.warning("Could not draft a follow-up for %s: %s", row["company"], exc)
            continue

        path = write_draft_file(
            cfg,
            company=row["company"],
            to=row["email"],
            subject=draft.subject,
            body=draft.body,
            resume=resume,
            source_url=row["source_url"] or "",
            stage=stage,
        )
        entry = {
            "date_added": date.today().strftime("%Y-%m-%d"),
            "first_contacted": row["date"],
            "stage": str(stage),
            "company": row["company"],
            "role": row["role"] or "",
            "contact_name": row["contact_name"] or "",
            "email": row["email"],
            "subject": draft.subject,
            "message": draft.body.replace("\r\n", "\n"),
            "draft_path": str(path),
            "sent": "no",
        }
        append_csv(ledger, FOLLOWUP_LEDGER_COLUMNS, entry)
        maybe_gmail_draft(
            cfg, to=row["email"], subject=draft.subject, body=draft.body, resume=resume
        )
        db.bump_outreach_followup(int(row["id"]))
        written.append(entry)
        log.info("Follow-up %d drafted for %s", stage, row["company"])

    return written
