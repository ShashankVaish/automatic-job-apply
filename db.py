"""SQLite tracking for every job seen, skipped, applied to or drafted."""
from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

STATUSES = ("applied", "skipped", "failed", "needs_review", "drafted")

EMAIL_STATUSES = ("queued", "sent", "failed", "replied", "bounced", "skipped")

CONTACT_ROLES = ("hr", "founder", "cofounder")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,            -- YYYY-MM-DD
    created_at      TEXT NOT NULL,            -- ISO timestamp
    site            TEXT NOT NULL,            -- linkedin | internshala | ...
    company         TEXT NOT NULL,
    role            TEXT NOT NULL,
    url             TEXT NOT NULL,
    apply_target    TEXT,                     -- board | career_page | email
    ats             TEXT,                     -- greenhouse | lever | ... | NULL
    match_score     INTEGER,
    resume_variant  TEXT,
    status          TEXT NOT NULL,
    error           TEXT,
    screenshot      TEXT,
    followup_added  INTEGER NOT NULL DEFAULT 0,
    description     TEXT,                     -- kept so outreach can tailor emails
    apply_email     TEXT                      -- when the posting says to email a CV
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_app_url ON applications(url);
CREATE INDEX IF NOT EXISTS idx_app_company_role ON applications(company, role);
CREATE INDEX IF NOT EXISTS idx_app_date ON applications(date);

CREATE TABLE IF NOT EXISTS site_blocks (
    site    TEXT NOT NULL,
    date    TEXT NOT NULL,
    reason  TEXT,
    PRIMARY KEY (site, date)
);

-- Contacts found for cold outreach. One row per email address.
-- `role` is hr | founder | cofounder. `verified` marks a contact you supplied
-- in inputs/contacts.csv, which is trusted without further checks.
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date_found      TEXT NOT NULL,
    company         TEXT NOT NULL,
    name            TEXT,
    role            TEXT NOT NULL,
    email           TEXT NOT NULL,
    source          TEXT,                     -- contacts_csv | job_post | careers_page | team_page | hunter
    source_url      TEXT,
    confidence      INTEGER,                  -- Hunter.io score, or 100 when verified
    verified        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',  -- active | bounced | blocked
    note            TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email ON contacts(lower(email));
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company);

-- Companies we could not find a work email for, so we don't retry endlessly.
CREATE TABLE IF NOT EXISTS contact_misses (
    company     TEXT NOT NULL,
    role        TEXT NOT NULL,
    date_tried  TEXT NOT NULL,
    reason      TEXT,
    PRIMARY KEY (company, role)
);

-- One row per email. Queued first, then sent, then possibly replied/bounced.
CREATE TABLE IF NOT EXISTS emails (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    date              TEXT NOT NULL,          -- queued date
    scheduled_for     TEXT NOT NULL,          -- earliest send date
    sent_at           TEXT,
    company           TEXT NOT NULL,
    recipient_email   TEXT NOT NULL,
    recipient_name    TEXT,
    recipient_role    TEXT NOT NULL,          -- hr | founder | cofounder
    job_role          TEXT,
    job_url           TEXT,
    subject           TEXT NOT NULL,
    body              TEXT NOT NULL,
    resume_variant    TEXT,
    status            TEXT NOT NULL,          -- queued | sent | failed | replied | bounced | skipped
    error             TEXT,
    gmail_message_id  TEXT,
    gmail_thread_id   TEXT,
    draft_path        TEXT,
    is_followup       INTEGER NOT NULL DEFAULT 0,
    followup_of       INTEGER,                -- emails.id of the original
    followup_sent     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);
CREATE INDEX IF NOT EXISTS idx_emails_company ON emails(company);
CREATE INDEX IF NOT EXISTS idx_emails_recipient ON emails(lower(recipient_email));
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date);

-- Companies that replied. Once here, all outreach to them stops.
CREATE TABLE IF NOT EXISTS replied_companies (
    company     TEXT PRIMARY KEY,
    domain      TEXT,
    date_found  TEXT NOT NULL,
    detail      TEXT
);
"""

CSV_COLUMNS = [
    "date",
    "site",
    "company",
    "role",
    "url",
    "apply_target",
    "ats",
    "match_score",
    "resume_variant",
    "status",
    "error",
    "screenshot",
]


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns that newer versions expect, for databases made earlier.

        SQLite has no "ADD COLUMN IF NOT EXISTS", so we read the table first.
        Keeps an existing tracking history usable after an upgrade.
        """
        wanted = {
            "applications": {
                "description": "TEXT",
                "apply_email": "TEXT",
            },
        }
        for table, columns in wanted.items():
            try:
                existing = {
                    row["name"]
                    for row in self.conn.execute(
                        "PRAGMA table_info(" + table + ")"
                    ).fetchall()
                }
            except Exception:
                continue
            for column, column_type in columns.items():
                if column in existing:
                    continue
                try:
                    self.conn.execute(
                        "ALTER TABLE {0} ADD COLUMN {1} {2}".format(
                            table, column, column_type
                        )
                    )
                except Exception:
                    pass

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- writing

    def record(
        self,
        *,
        site: str,
        company: str,
        role: str,
        url: str,
        status: str,
        apply_target: str | None = None,
        ats: str | None = None,
        match_score: int | None = None,
        resume_variant: str | None = None,
        error: str | None = None,
        screenshot: str | None = None,
        description: str | None = None,
        apply_email: str | None = None,
    ) -> int:
        """Insert or update the row for this job URL. One row per job."""
        if status not in STATUSES:
            raise ValueError("unknown status: " + str(status))
        now = datetime.now()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO applications
                    (date, created_at, site, company, role, url, apply_target, ats,
                     match_score, resume_variant, status, error, screenshot,
                     description, apply_email)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    date=excluded.date,
                    created_at=excluded.created_at,
                    apply_target=COALESCE(excluded.apply_target, applications.apply_target),
                    ats=COALESCE(excluded.ats, applications.ats),
                    match_score=COALESCE(excluded.match_score, applications.match_score),
                    resume_variant=COALESCE(excluded.resume_variant, applications.resume_variant),
                    status=excluded.status,
                    error=excluded.error,
                    screenshot=COALESCE(excluded.screenshot, applications.screenshot),
                    description=COALESCE(excluded.description, applications.description),
                    apply_email=COALESCE(excluded.apply_email, applications.apply_email)
                """,
                (
                    now.strftime("%Y-%m-%d"),
                    now.isoformat(timespec="seconds"),
                    site,
                    company,
                    role,
                    url,
                    apply_target,
                    ats,
                    match_score,
                    resume_variant,
                    status,
                    error,
                    screenshot,
                    (description or "")[:8000] or None,
                    apply_email,
                ),
            )
            return int(cur.lastrowid or 0)

    def block_site(self, site: str, reason: str) -> None:
        """Stop touching this site for the rest of today."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO site_blocks(site, date, reason) VALUES (?,?,?)",
                (site, date.today().strftime("%Y-%m-%d"), reason),
            )

    def is_blocked(self, site: str) -> str | None:
        row = self.conn.execute(
            "SELECT reason FROM site_blocks WHERE site=? AND date=?",
            (site, date.today().strftime("%Y-%m-%d")),
        ).fetchone()
        return row["reason"] if row else None

    # ---------------------------------------------------------------- reading

    def seen_url(self, url: str) -> str | None:
        """Return the recorded status if this exact URL was handled before."""
        row = self.conn.execute(
            "SELECT status FROM applications WHERE url=?", (url,)
        ).fetchone()
        return row["status"] if row else None

    def duplicate_company_role(
        self, company: str, role: str, within_days: int
    ) -> sqlite3.Row | None:
        """Same company + title inside the dedupe window counts as a duplicate."""
        cutoff = (date.today() - timedelta(days=within_days)).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT * FROM applications WHERE date >= ? AND status != 'skipped'",
            (cutoff,),
        ).fetchall()
        c, r = _norm(company), _norm(role)
        for row in rows:
            if _norm(row["company"]) == c and _norm(row["role"]) == r:
                return row
        return None

    def count_today(self, site: str) -> int:
        """Submitted-or-drafted count for today; skips don't burn the daily limit."""
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM applications
            WHERE date=? AND site=? AND status IN ('applied','drafted','needs_review')
            """,
            (date.today().strftime("%Y-%m-%d"), site),
        ).fetchone()
        return int(row["n"])

    def count_today_career_pages(self) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM applications
            WHERE date=? AND apply_target='career_page'
              AND status IN ('applied','drafted','needs_review')
            """,
            (date.today().strftime("%Y-%m-%d"),),
        ).fetchone()
        return int(row["n"])

    def recent(self, days: int = 7) -> list[sqlite3.Row]:
        cutoff = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return self.conn.execute(
            "SELECT * FROM applications WHERE date >= ? ORDER BY date DESC, id DESC",
            (cutoff,),
        ).fetchall()

    def all_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM applications ORDER BY date, id"
        ).fetchall()

    # -------------------------------------------------------------- contacts

    def add_contact(
        self,
        *,
        company: str,
        role: str,
        email: str,
        name: str = "",
        source: str = "",
        source_url: str = "",
        confidence: int | None = None,
        verified: bool = False,
        note: str = "",
    ) -> int:
        """Record a contact. Existing rows are enriched, never downgraded."""
        if role not in CONTACT_ROLES:
            raise ValueError("unknown contact role: " + str(role))
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO contacts
                    (date_found, company, name, role, email, source, source_url,
                     confidence, verified, status, note)
                VALUES (?,?,?,?,?,?,?,?,?,'active',?)
                ON CONFLICT(lower(email)) DO UPDATE SET
                    name=COALESCE(NULLIF(excluded.name,''), contacts.name),
                    company=COALESCE(NULLIF(excluded.company,''), contacts.company),
                    source_url=COALESCE(NULLIF(excluded.source_url,''), contacts.source_url),
                    confidence=MAX(
                        COALESCE(excluded.confidence, 0), COALESCE(contacts.confidence, 0)
                    ),
                    verified=MAX(excluded.verified, contacts.verified),
                    note=COALESCE(NULLIF(excluded.note,''), contacts.note)
                """,
                (
                    date.today().strftime("%Y-%m-%d"),
                    company,
                    name,
                    role,
                    email.strip().lower(),
                    source,
                    source_url,
                    confidence,
                    1 if verified else 0,
                    note,
                ),
            )
            return int(cur.lastrowid or 0)

    def contact(self, email: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM contacts WHERE lower(email)=lower(?)", (email,)
        ).fetchone()

    def contacts_for(self, company: str, *, active_only: bool = True) -> list[sqlite3.Row]:
        """Contacts at one company, HR first then founder then co-founder."""
        rows = self.conn.execute("SELECT * FROM contacts").fetchall()
        target = _norm(company)
        out = [r for r in rows if _norm(r["company"]) == target]
        if active_only:
            out = [r for r in out if r["status"] == "active"]
        order = {role: i for i, role in enumerate(CONTACT_ROLES)}
        out.sort(key=lambda r: (order.get(r["role"], 9), -(r["confidence"] or 0)))
        return out

    def mark_contact(self, email: str, status: str, note: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE contacts SET status=?, note=COALESCE(NULLIF(?,''), note) "
                "WHERE lower(email)=lower(?)",
                (status, note, email),
            )

    def record_contact_miss(self, company: str, role: str, reason: str) -> None:
        """Remember that no work email could be found, so we don't retry daily."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO contact_misses(company, role, date_tried, reason) "
                "VALUES (?,?,?,?)",
                (company, role, date.today().strftime("%Y-%m-%d"), reason),
            )

    def contact_miss(self, company: str, role: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM contact_misses WHERE company=? AND role=?", (company, role)
        ).fetchone()

    def all_contacts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM contacts ORDER BY company, role").fetchall()

    # ---------------------------------------------------------------- emails

    def queue_email(
        self,
        *,
        company: str,
        recipient_email: str,
        recipient_role: str,
        subject: str,
        body: str,
        recipient_name: str = "",
        job_role: str = "",
        job_url: str = "",
        resume_variant: str = "",
        scheduled_for: str = "",
        draft_path: str = "",
        is_followup: bool = False,
        followup_of: int | None = None,
    ) -> int:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO emails
                    (created_at, date, scheduled_for, company, recipient_email,
                     recipient_name, recipient_role, job_role, job_url, subject, body,
                     resume_variant, status, draft_path, is_followup, followup_of)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)
                """,
                (
                    now.isoformat(timespec="seconds"),
                    today,
                    scheduled_for or today,
                    company,
                    recipient_email.strip().lower(),
                    recipient_name,
                    recipient_role,
                    job_role,
                    job_url,
                    subject,
                    body,
                    resume_variant,
                    draft_path,
                    1 if is_followup else 0,
                    followup_of,
                ),
            )
            return int(cur.lastrowid or 0)

    def email_row(self, email_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()

    def update_email(self, email_id: int, **fields: Any) -> None:
        allowed = {
            "status", "error", "gmail_message_id", "gmail_thread_id", "sent_at",
            "subject", "body", "scheduled_for", "followup_sent", "draft_path",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(k + "=?" for k in updates)
        with self.conn:
            self.conn.execute(
                "UPDATE emails SET " + assignments + " WHERE id=?",
                (*updates.values(), email_id),
            )

    def queued_emails(self, *, due_only: bool = True) -> list[sqlite3.Row]:
        """Queued emails, HR before founder/cofounder, oldest schedule first."""
        rows = self.conn.execute(
            "SELECT * FROM emails WHERE status='queued' ORDER BY scheduled_for, id"
        ).fetchall()
        if due_only:
            today = date.today().strftime("%Y-%m-%d")
            rows = [r for r in rows if (r["scheduled_for"] or today) <= today]
        order = {role: i for i, role in enumerate(CONTACT_ROLES)}
        return sorted(
            rows,
            key=lambda r: (
                r["scheduled_for"] or "",
                order.get(r["recipient_role"], 9),
                r["id"],
            ),
        )

    def emailed_recently(self, address: str, within_days: int) -> sqlite3.Row | None:
        """Any first-contact email sent to this address inside the window."""
        cutoff = (date.today() - timedelta(days=within_days)).strftime("%Y-%m-%d")
        return self.conn.execute(
            """
            SELECT * FROM emails
            WHERE lower(recipient_email)=lower(?) AND is_followup=0
              AND status IN ('sent','replied','bounced')
              AND substr(COALESCE(sent_at, date),1,10) >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (address, cutoff),
        ).fetchone()

    def has_email_for(self, address: str, *, include_queued: bool = True) -> bool:
        """Max one email per person: has this address been queued or contacted?"""
        statuses = (
            "('queued','sent','replied','bounced')"
            if include_queued
            else "('sent','replied','bounced')"
        )
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM emails WHERE lower(recipient_email)=lower(?) "
            "AND is_followup=0 AND status IN " + statuses,
            (address,),
        ).fetchone()
        return int(row["n"]) > 0

    def company_email_count(self, company: str) -> int:
        """How many distinct people at this company are queued or contacted."""
        rows = self.conn.execute(
            "SELECT company, recipient_email FROM emails "
            "WHERE is_followup=0 AND status IN ('queued','sent','replied','bounced')"
        ).fetchall()
        target = _norm(company)
        return len({r["recipient_email"] for r in rows if _norm(r["company"]) == target})

    def count_sent_today(self) -> int:
        today = date.today().strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM emails "
            "WHERE substr(COALESCE(sent_at, ''),1,10)=? "
            "AND status IN ('sent','replied','bounced')",
            (today,),
        ).fetchone()
        return int(row["n"])

    def mark_sent(self, email_id: int, *, message_id: str = "", thread_id: str = "") -> None:
        self.update_email(
            email_id,
            status="sent",
            sent_at=datetime.now().isoformat(timespec="seconds"),
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            error=None,
        )

    # ------------------------------------------------------ replies/bounces

    def mark_company_replied(self, company: str, domain: str = "", detail: str = "") -> None:
        """A reply stops all outreach to that company, queued included."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO replied_companies"
                "(company, domain, date_found, detail) VALUES (?,?,?,?)",
                (company, domain, date.today().strftime("%Y-%m-%d"), detail),
            )
            self.conn.execute(
                "UPDATE emails SET status='replied' WHERE company=? AND status='sent'",
                (company,),
            )
            self.conn.execute(
                "UPDATE emails SET status='skipped', error='company replied' "
                "WHERE company=? AND status='queued'",
                (company,),
            )

    def company_replied(self, company: str) -> sqlite3.Row | None:
        rows = self.conn.execute("SELECT * FROM replied_companies").fetchall()
        target = _norm(company)
        for row in rows:
            if _norm(row["company"]) == target:
                return row
        return None

    def mark_bounced(self, address: str, detail: str = "") -> None:
        """A bounced address is never used again."""
        with self.conn:
            self.conn.execute(
                "UPDATE emails SET status='bounced', error=? "
                "WHERE lower(recipient_email)=lower(?) AND status IN ('sent','queued')",
                (detail or "bounced", address),
            )
        self.mark_contact(address, "bounced", detail or "bounced")

    # ----------------------------------------------------------- follow-ups

    def emails_due_followup(self, days_after: int) -> list[sqlite3.Row]:
        """Sent HR emails with no reply, old enough for the single follow-up."""
        cutoff = (date.today() - timedelta(days=days_after)).strftime("%Y-%m-%d")
        return self.conn.execute(
            """
            SELECT * FROM emails
            WHERE status='sent' AND is_followup=0 AND followup_sent=0
              AND recipient_role='hr'
              AND substr(COALESCE(sent_at, date),1,10) <= ?
            ORDER BY id
            """,
            (cutoff,),
        ).fetchall()

    def mark_followup_queued(self, email_id: int) -> None:
        self.update_email(email_id, followup_sent=1)

    # -------------------------------------------------------------- reports

    def email_rows(self, days: int | None = None) -> list[sqlite3.Row]:
        if days is None:
            return self.conn.execute(
                "SELECT * FROM emails ORDER BY date DESC, id DESC"
            ).fetchall()
        cutoff = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return self.conn.execute(
            "SELECT * FROM emails WHERE date >= ? ORDER BY date DESC, id DESC",
            (cutoff,),
        ).fetchall()

    def email_stats(self, days: int = 30) -> dict[str, Any]:
        rows = self.email_rows(days)
        counts: dict[str, Any] = {status: 0 for status in EMAIL_STATUSES}
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] += 1
        delivered = counts["sent"] + counts["replied"]
        counts["total"] = len(rows)
        counts["reply_rate"] = (
            round(100.0 * counts["replied"] / delivered, 1) if delivered else 0.0
        )
        return counts

    def matched_jobs_for_outreach(self, threshold: int, days: int = 30) -> list[sqlite3.Row]:
        """Jobs that scored at or above the threshold - the outreach source."""
        cutoff = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return self.conn.execute(
            """
            SELECT * FROM applications
            WHERE date >= ? AND match_score >= ? AND status != 'skipped'
            ORDER BY match_score DESC, id DESC
            """,
            (cutoff, threshold),
        ).fetchall()

    # ---------------------------------------------------------------- export

    def export_csv(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in self.all_rows():
                writer.writerow({k: row[k] for k in CSV_COLUMNS})
        return out


def append_csv(path: str | Path, columns: Iterable[str], row: dict[str, Any]) -> None:
    """Append one row, writing a header if the file is new. Used for the outreach ledger."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists() or out.stat().st_size == 0
    with out.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def open_db(path: str | Path) -> Database:
    return Database(path)


__all__ = ["Database", "open_db", "append_csv", "closing", "STATUSES", "CSV_COLUMNS"]
