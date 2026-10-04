"""SQLite tracking for every job seen, skipped, applied to or drafted."""
from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

STATUSES = ("applied", "skipped", "failed", "needs_review", "drafted")

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
    followup_added  INTEGER NOT NULL DEFAULT 0
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
        self.conn.commit()

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
                     match_score, resume_variant, status, error, screenshot)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    date=excluded.date,
                    created_at=excluded.created_at,
                    apply_target=COALESCE(excluded.apply_target, applications.apply_target),
                    ats=COALESCE(excluded.ats, applications.ats),
                    match_score=COALESCE(excluded.match_score, applications.match_score),
                    resume_variant=COALESCE(excluded.resume_variant, applications.resume_variant),
                    status=excluded.status,
                    error=excluded.error,
                    screenshot=COALESCE(excluded.screenshot, applications.screenshot)
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

    def mark_followup_added(self, app_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE applications SET followup_added=1 WHERE id=?", (app_id,)
            )

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

    def due_followups(self, days_after: int) -> list[sqlite3.Row]:
        target = (date.today() - timedelta(days=days_after)).strftime("%Y-%m-%d")
        return self.conn.execute(
            """
            SELECT * FROM applications
            WHERE status='applied' AND followup_added=0 AND date <= ?
            ORDER BY date
            """,
            (target,),
        ).fetchall()

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
    """Append one row, writing a header if the file is new. Used for followups.csv."""
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
