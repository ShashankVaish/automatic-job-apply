"""Configuration loading: config.yaml for everything about you, .env for secrets."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "gemini-2.5-flash"


class ResumeVariant(BaseModel):
    name: str
    pdf_path: str
    public_link: str = ""
    focus_keywords: list[str] = Field(default_factory=list)

    @property
    def abs_pdf_path(self) -> Path:
        p = Path(self.pdf_path)
        return p if p.is_absolute() else (ROOT / p).resolve()

    def exists(self) -> bool:
        return self.abs_pdf_path.is_file()


class Profile(BaseModel):
    name: str
    email: str
    phone: str
    location: str = ""
    open_to_remote: bool = True
    willing_to_relocate: bool = True
    graduation_year: int | None = None
    degree: str = ""
    college: str = ""
    experience_level: str = "fresher"
    years_of_experience: int = 0
    expected_salary: str = ""
    expected_stipend: str = ""
    notice_period: str = "Immediate"
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    work_authorization: str = ""
    requires_sponsorship: bool = False
    target_roles: list[str] = Field(default_factory=list)
    target_locations: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)

    def first_name(self) -> str:
        return self.name.split()[0] if self.name.strip() else ""

    def last_name(self) -> str:
        parts = self.name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    def as_prompt_block(self) -> str:
        """Compact, factual profile for Gemini. Only real facts go in here."""
        remote = "yes" if self.open_to_remote else "no"
        relocate = "yes" if self.willing_to_relocate else "no"
        sponsor = "yes" if self.requires_sponsorship else "no"
        return "\n".join(
            [
                "Name: " + self.name,
                "Email: " + self.email,
                "Phone: " + self.phone,
                "Current location: " + self.location,
                "Open to remote: " + remote,
                "Willing to relocate: " + relocate,
                "Degree: {0} from {1}, graduating {2}".format(
                    self.degree, self.college, self.graduation_year
                ),
                "Experience level: {0} ({1} years of full-time experience)".format(
                    self.experience_level, self.years_of_experience
                ),
                "Expected salary: {0}; expected stipend: {1}".format(
                    self.expected_salary, self.expected_stipend
                ),
                "Notice period: " + self.notice_period,
                "Work authorization: " + self.work_authorization,
                "Needs visa sponsorship: " + sponsor,
                "Skills: " + ", ".join(self.skills),
                "Target roles: " + ", ".join(self.target_roles),
                "Target locations: " + ", ".join(self.target_locations),
                "LinkedIn: {0} | GitHub: {1} | Portfolio: {2}".format(
                    self.linkedin, self.github, self.portfolio
                ),
            ]
        )


class BrowserCfg(BaseModel):
    mode: str = "persistent"
    persistent_profile_dir: str = "./browser_profile"
    cdp_url: str = "http://localhost:9222"
    slow_mo_ms: int = 120
    nav_timeout_ms: int = 45000

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        if v not in ("persistent", "cdp"):
            raise ValueError("browser.mode must be 'persistent' or 'cdp'")
        return v


class MatchingCfg(BaseModel):
    match_threshold: int = 70
    auto_threshold: int = 80
    dedupe_days: int = 30


class SourceCfg(BaseModel):
    enabled: bool = False
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    max_jobs_per_search: int = 20
    date_posted: str = "week"
    experience_years: int = 0


class RunWindow(BaseModel):
    timezone: str = "Asia/Kolkata"
    start_hour: int = 9
    end_hour: int = 21
    enforce: bool = True


class Delays(BaseModel):
    action_min_s: float = 3
    action_max_s: float = 10
    between_apps_min_s: float = 45
    between_apps_max_s: float = 120


class LimitsCfg(BaseModel):
    daily: dict[str, int] = Field(default_factory=dict)
    run_window: RunWindow = Field(default_factory=RunWindow)
    delays: Delays = Field(default_factory=Delays)

    def daily_cap(self, site: str) -> int:
        return int(self.daily.get(site, 10_000))


class EmailCfg(BaseModel):
    drafts_dir: str = "./drafts"
    gmail_api: bool = False


class OutreachCfg(BaseModel):
    enabled: bool = True
    outbox_dir: str = "./outbox"
    companies_csv: str = "./inputs/companies.csv"
    job_urls: str = "./inputs/job_urls.txt"
    dedupe_days: int = 90
    max_followups: int = 2
    follow_contact_page: bool = True


class FollowupCfg(BaseModel):
    days_after: int = 6
    csv_path: str = "./followups.csv"


class PathsCfg(BaseModel):
    db: str = "./data/applications.db"
    logs_dir: str = "./logs"
    screenshots_dir: str = "./screenshots"
    applications_csv: str = "./applications.csv"


class Config(BaseModel):
    profile: Profile
    resumes: list[ResumeVariant]
    browser: BrowserCfg = Field(default_factory=BrowserCfg)
    matching: MatchingCfg = Field(default_factory=MatchingCfg)
    sources: dict[str, SourceCfg] = Field(default_factory=dict)
    limits: LimitsCfg = Field(default_factory=LimitsCfg)
    email: EmailCfg = Field(default_factory=EmailCfg)
    outreach: OutreachCfg = Field(default_factory=OutreachCfg)
    followups: FollowupCfg = Field(default_factory=FollowupCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)

    # Filled from .env, never from config.yaml.
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_MODEL

    def resume(self, name: str) -> ResumeVariant:
        for r in self.resumes:
            if r.name.lower() == (name or "").lower():
                return r
        return self.resumes[0]

    def source(self, name: str) -> SourceCfg:
        return self.sources.get(name) or SourceCfg()

    def abs_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (ROOT / p).resolve()


def _unfilled(cfg: Config) -> list[str]:
    """Find [placeholders] the user forgot to replace."""
    bad: list[str] = []

    def check(label: str, value: Any) -> None:
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("[") and s.endswith("]"):
                bad.append(label)

    # Every profile string that can end up typed into a real application form.
    # A leftover "[https://github.com/you]" in a submitted form is worse than
    # an empty field, so these are all checked.
    for field in (
        "name",
        "email",
        "phone",
        "location",
        "degree",
        "college",
        "linkedin",
        "github",
        "portfolio",
        "expected_salary",
        "expected_stipend",
        "notice_period",
    ):
        check("profile." + field, getattr(cfg.profile, field))

    for i, r in enumerate(cfg.resumes):
        check("resumes[{0}].public_link".format(i), r.public_link)

    for key, values in (
        ("target_roles", cfg.profile.target_roles),
        ("target_locations", cfg.profile.target_locations),
        ("skills", cfg.profile.skills),
    ):
        for j, value in enumerate(values):
            check("profile.{0}[{1}]".format(key, j), value)

    for name, source in cfg.sources.items():
        if not source.enabled:
            continue
        for j, value in enumerate(source.keywords):
            check("sources.{0}.keywords[{1}]".format(name, j), value)
        for j, value in enumerate(source.locations):
            check("sources.{0}.locations[{1}]".format(name, j), value)

    return bad


def load_config(path: str | Path = "config.yaml", *, strict: bool = True) -> Config:
    load_dotenv(ROOT / ".env")

    cfg_path = Path(path)
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path
    if not cfg_path.is_file():
        raise SystemExit(
            "No config found at {0}.\n".format(cfg_path)
            + "Copy config.yaml.example to config.yaml and fill it in "
            "(see the Setup section of README.md)."
        )

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = Config(**raw)
    cfg.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    cfg.gemini_model = (os.getenv("GEMINI_MODEL") or DEFAULT_MODEL).strip()

    if strict:
        missing = _unfilled(cfg)
        if missing:
            raise SystemExit(
                "config.yaml still has placeholder values in:\n  - "
                + "\n  - ".join(missing)
                + "\nReplace the [bracketed] text with your real details."
            )
        if not cfg.gemini_api_key:
            raise SystemExit(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and paste your key "
                "from https://aistudio.google.com/app/apikey"
            )

    for d in (cfg.paths.logs_dir, cfg.paths.screenshots_dir, cfg.email.drafts_dir):
        cfg.abs_path(d).mkdir(parents=True, exist_ok=True)
    cfg.abs_path(cfg.paths.db).parent.mkdir(parents=True, exist_ok=True)
    return cfg
