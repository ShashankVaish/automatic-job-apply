"""Shared test fixtures.

Tests never touch the network, never log in anywhere and never submit anything.
Form filling is exercised against the local HTML files in tests/fixtures/ via
file:// URLs, with a headless browser (the real app is always headed).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Smallest thing a PDF reader will accept. Enough for set_input_files().
MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def fixture_url(name: str) -> str:
    path = FIXTURES / name
    assert path.is_file(), "missing fixture: " + str(path)
    return path.as_uri()


@pytest.fixture
def config_dict(tmp_path: Path) -> dict:
    """A fully filled config, so nothing trips the placeholder check."""
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir()
    for name in ("frontend", "data", "general"):
        (resumes_dir / (name + ".pdf")).write_bytes(MINIMAL_PDF)

    return {
        "profile": {
            "name": "Asha Verma",
            "email": "asha.verma@example.com",
            "phone": "+91 98765 43210",
            "location": "Noida",
            "open_to_remote": True,
            "willing_to_relocate": True,
            "graduation_year": 2026,
            "degree": "B.Tech Computer Science",
            "college": "Fixture Institute of Technology",
            "experience_level": "fresher",
            "years_of_experience": 0,
            "expected_salary": "6 LPA",
            "expected_stipend": "25000/month",
            "notice_period": "Immediate",
            "linkedin": "https://linkedin.com/in/ashaverma",
            "github": "https://github.com/ashaverma",
            "portfolio": "https://asha.dev",
            "work_authorization": "Authorized to work in India without sponsorship",
            "requires_sponsorship": False,
            "target_roles": ["Software Engineer Intern", "Frontend Developer"],
            "target_locations": ["Noida", "Remote"],
            "skills": ["Python", "JavaScript", "React", "SQL"],
        },
        "resumes": [
            {
                "name": "frontend",
                "pdf_path": str(resumes_dir / "frontend.pdf"),
                "public_link": "https://drive.google.com/file/d/FRONTEND/view",
                "focus_keywords": ["react", "javascript", "ui", "css", "frontend"],
            },
            {
                "name": "data",
                "pdf_path": str(resumes_dir / "data.pdf"),
                "public_link": "https://drive.google.com/file/d/DATA/view",
                "focus_keywords": ["sql", "pandas", "analytics", "data"],
            },
            {
                "name": "general",
                "pdf_path": str(resumes_dir / "general.pdf"),
                "public_link": "https://drive.google.com/file/d/GENERAL/view",
                "focus_keywords": ["software", "engineer", "intern"],
            },
        ],
        "browser": {"mode": "persistent", "slow_mo_ms": 0},
        "matching": {"match_threshold": 70, "auto_threshold": 80, "dedupe_days": 30},
        "sources": {
            "internshala": {
                "enabled": True,
                "keywords": ["web development"],
                "locations": ["work-from-home"],
                "max_jobs_per_search": 5,
            },
            "linkedin": {"enabled": False, "keywords": ["intern"], "locations": ["Noida"]},
            "naukri": {"enabled": False},
            "indeed": {"enabled": False},
        },
        "limits": {
            "daily": {
                "linkedin": 15,
                "naukri": 15,
                "indeed": 20,
                "internshala": 25,
                "career_pages": 40,
            },
            # Tests must not depend on the wall clock.
            "run_window": {
                "timezone": "Asia/Kolkata",
                "start_hour": 9,
                "end_hour": 21,
                "enforce": False,
            },
            # No human-paced delays in tests.
            "delays": {
                "action_min_s": 0,
                "action_max_s": 0,
                "between_apps_min_s": 0,
                "between_apps_max_s": 0,
            },
        },
        "email": {"drafts_dir": str(tmp_path / "drafts"), "gmail_api": False},
        "outreach": {
            "enabled": True,
            # Isolated per test, so no test can write into the real ./outbox.
            "outbox_dir": str(tmp_path / "outbox"),
            "companies_csv": "./inputs/companies.csv",
            "job_urls": "./inputs/job_urls.txt",
            "dedupe_days": 90,
            "max_followups": 2,
            "follow_contact_page": True,
        },
        "followups": {"days_after": 6, "csv_path": str(tmp_path / "followups.csv")},
        "paths": {
            "db": str(tmp_path / "data" / "applications.db"),
            "logs_dir": str(tmp_path / "logs"),
            "screenshots_dir": str(tmp_path / "screenshots"),
            "applications_csv": str(tmp_path / "applications.csv"),
        },
    }


@pytest.fixture
def config_path(tmp_path: Path, config_dict: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_dict, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def cfg(config_path: Path, monkeypatch):
    from config import load_config

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GEMINI_MODEL", "mock")
    return load_config(config_path, strict=True)


@pytest.fixture
def db(cfg):
    from db import open_db

    database = open_db(cfg.abs_path(cfg.paths.db))
    yield database
    database.close()


@pytest.fixture
def gemini(cfg):
    from tests.mock_gemini import MockGemini

    return MockGemini(cfg)


@pytest.fixture(scope="session")
def browser():
    """One headless browser for the whole test session."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


def block_network(context) -> list[str]:
    """Abort every request that isn't a local file.

    Tests must never touch a real job site. This also catches the subtler bug
    of a relative link being resolved against a live domain by mistake: the
    request is aborted and recorded here instead of quietly succeeding.
    """
    attempted: list[str] = []

    def handler(route, request):
        url = request.url
        if url.startswith("file://") or url.startswith("data:") or url.startswith("about:"):
            route.continue_()
            return
        attempted.append(url)
        route.abort()

    context.route("**/*", handler)
    return attempted


@pytest.fixture
def offline_context(browser):
    """A browser context that physically cannot reach the network."""
    context = browser.new_context(accept_downloads=True)
    attempted = block_network(context)
    yield context, attempted
    context.close()


@pytest.fixture
def page(offline_context):
    context, attempted = offline_context
    p = context.new_page()
    p.set_default_timeout(5000)
    yield p
    assert not attempted, "a test tried to reach the network: " + ", ".join(attempted[:5])


@pytest.fixture
def pacer():
    from browser.human import Pacer

    return Pacer(0, 0, 0, 0, enabled=False)
