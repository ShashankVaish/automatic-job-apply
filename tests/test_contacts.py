"""Contact discovery for cold outreach (spec 8.2).

The two rules that matter most, and are tested hardest:
  * emails are never guessed from a pattern
  * personal (non-company) addresses are never used
"""
from __future__ import annotations

import pytest

from contacts import (
    PERSONAL_DOMAINS,
    ROLE_COFOUNDER,
    ROLE_FOUNDER,
    ROLE_HR,
    Contact,
    best_hr_address,
    contacts_from_job_post,
    discover_contacts,
    founders_from_company_site,
    hr_from_company_site,
    hunter_find,
    is_blocked,
    is_junk,
    is_personal_domain,
    is_usable_work_email,
    load_blocklist,
    load_contacts_csv,
    site_domain,
)
from tests.conftest import fixture_url
from tests.mock_gmail import MockHunter


# ------------------------------------------------------- address validation


@pytest.mark.parametrize(
    "email",
    [
        "someone@gmail.com",
        "someone@yahoo.co.in",
        "someone@outlook.com",
        "someone@hotmail.com",
        "someone@protonmail.com",
        "someone@rediffmail.com",
    ],
)
def test_personal_addresses_are_rejected(email):
    assert is_personal_domain(email) is True
    assert is_usable_work_email(email) is False


@pytest.mark.parametrize(
    "email",
    [
        "noreply@fixture.example",
        "postmaster@fixture.example",
        "sales@fixture.example",
        "billing@fixture.example",
        "legal@fixture.example",
        "press@fixture.example",
        "unsubscribe@fixture.example",
        "grievance@fixture.example",
    ],
)
def test_junk_mailboxes_are_rejected(email):
    assert is_junk(email) is True
    assert is_usable_work_email(email) is False


@pytest.mark.parametrize(
    "email",
    [
        "hr@fixture.example",
        "careers@fixture.example",
        "priya.nair@fixture.example",
        "talent@fixture.co.in",
    ],
)
def test_work_addresses_are_accepted(email):
    assert is_usable_work_email(email) is True


@pytest.mark.parametrize(
    "bad",
    ["", "not-an-email", "two@@at.example", "nodomain@", "@nolocal.example",
     "image@fixture.png"],
)
def test_malformed_addresses_are_rejected(bad):
    assert is_usable_work_email(bad) is False


def test_hr_inboxes_are_preferred_over_general_ones():
    assert (
        best_hr_address(["hello@fixture.example", "careers@fixture.example"])
        == "careers@fixture.example"
    )
    assert (
        best_hr_address(["info@fixture.example", "hr@fixture.example"])
        == "hr@fixture.example"
    )


def test_no_usable_address_returns_empty():
    assert best_hr_address([]) == ""
    assert best_hr_address(["noreply@x.example", "sales@x.example"]) == ""
    assert best_hr_address(["someone@gmail.com"]) == ""


def test_personal_domain_list_covers_the_common_ones():
    for domain in ("gmail.com", "outlook.com", "yahoo.com", "icloud.com"):
        assert domain in PERSONAL_DOMAINS


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.fixturelabs.example/careers", "fixturelabs.example"),
        ("http://fixturelabs.example", "fixturelabs.example"),
        ("https://fixturelabs.example:8080/x", "fixturelabs.example"),
    ],
)
def test_site_domain(url, expected):
    assert site_domain(url) == expected


# ----------------------------------------------- 1. inputs/contacts.csv


def test_loads_your_verified_contacts(cfg, inputs_dir):
    path = cfg.abs_path(cfg.outreach.contacts_csv)
    path.write_text(
        "company,name,role,email,source_url,notes\n"
        "Fixture Labs,Priya Nair,hr,priya.nair@fixturelabs.example,https://x,met at meetup\n"
        "Fixture Labs,Arjun Mehta,founder,arjun@fixturelabs.example,,\n"
        "Fixture Labs,Dev Rao,co-founder,dev@fixturelabs.example,,\n",
        encoding="utf-8",
    )
    loaded = load_contacts_csv(path)

    assert len(loaded) == 3
    assert loaded[0].role == ROLE_HR
    assert loaded[1].role == ROLE_FOUNDER
    # "co-founder" normalises to cofounder.
    assert loaded[2].role == ROLE_COFOUNDER
    # Your own list is trusted without further checks.
    assert all(c.verified for c in loaded)
    assert all(c.confidence == 100 for c in loaded)
    assert all(c.source == "contacts_csv" for c in loaded)


def test_contacts_csv_defaults_to_hr_for_an_unknown_role(cfg, inputs_dir):
    path = cfg.abs_path(cfg.outreach.contacts_csv)
    path.write_text(
        "company,name,role,email\nFixture Labs,X,chief vibes officer,x@fixture.example\n",
        encoding="utf-8",
    )
    assert load_contacts_csv(path)[0].role == ROLE_HR


def test_missing_contacts_csv_is_not_an_error(cfg):
    assert load_contacts_csv(cfg.abs_path("./inputs/definitely-not-here.csv")) == []


# ------------------------------------------------------------- blocklist


def test_blocklist_blocks_addresses_and_whole_domains(cfg, inputs_dir):
    path = cfg.abs_path(cfg.outreach.blocklist)
    path.write_text(
        "# people I already annoyed\n"
        "someone@fixture.example\n"
        "\n"
        "competitor.example\n"
        "@another.example\n",
        encoding="utf-8",
    )
    blocked = load_blocklist(path)

    assert is_blocked("someone@fixture.example", blocked) is True
    assert is_blocked("SOMEONE@FIXTURE.EXAMPLE", blocked) is True
    assert is_blocked("anyone@competitor.example", blocked) is True
    assert is_blocked("anyone@another.example", blocked) is True
    assert is_blocked("hr@fixturelabs.example", blocked) is False


def test_an_empty_address_is_treated_as_blocked():
    assert is_blocked("", set()) is True


# --------------------------------------------------- 2. the job post


def test_an_address_in_the_job_post_becomes_the_hr_contact():
    found = contacts_from_job_post(
        "Fixture Media",
        "Send your resume to careers@fixturemedia.example to apply.",
        "https://example.com/job/1",
    )
    assert len(found) == 1
    assert found[0].email == "careers@fixturemedia.example"
    assert found[0].role == ROLE_HR
    assert found[0].source == "job_post"


def test_a_personal_address_in_the_job_post_is_ignored():
    assert contacts_from_job_post("X", "Mail me at someone@gmail.com") == []


def test_a_noreply_address_in_the_job_post_is_ignored():
    assert contacts_from_job_post("X", "Replies go to noreply@x.example") == []


# ------------------------------------------- 3/4. the company's own site


def test_finds_a_hiring_address_on_the_careers_page(page, cfg):
    found, notes = hr_from_company_site(
        page, "Fixture Robotics", fixture_url("company_site.html")
    )
    assert len(found) == 1
    assert found[0].email == "careers@fixturerobotics.example"
    assert found[0].role == ROLE_HR
    assert found[0].source == "careers_page"


def test_finds_founder_and_cofounder_with_published_emails(page, cfg):
    found, notes = founders_from_company_site(
        page, "Fixture Robotics", fixture_url("company_site.html")
    )
    roles = {c.role for c in found}
    assert ROLE_FOUNDER in roles

    founder = next(c for c in found if c.role == ROLE_FOUNDER)
    assert founder.email.endswith("@fixturerobotics.example")
    assert founder.name
    assert founder.source == "team_page"


def test_a_site_with_no_published_address_yields_nothing(page, cfg):
    found, notes = hr_from_company_site(
        page, "Fixture Silent", fixture_url("company_site_no_email.html")
    )
    assert found == []
    assert any("no hiring address" in n for n in notes)


def test_a_founder_with_only_a_personal_address_is_not_used(page, cfg):
    """A gmail address on the team page is still not a work email."""
    found, notes = founders_from_company_site(
        page, "Fixture Silent", fixture_url("company_site_no_email.html")
    )
    assert all(not is_personal_domain(c.email) for c in found)
    assert found == []


def test_an_unreachable_site_does_not_raise(page, cfg):
    found, notes = hr_from_company_site(page, "X", "file:///nope/not-here.html")
    assert found == []


# --------------------------------------------------------- 5. Hunter.io


def test_hunter_is_off_by_default(cfg, monkeypatch):
    from contacts import hunter_enabled

    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    assert cfg.outreach.use_hunter is False
    assert hunter_enabled(cfg) is False


def test_hunter_needs_both_the_flag_and_a_key(cfg, monkeypatch):
    from contacts import hunter_enabled

    cfg.outreach.use_hunter = True
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    assert hunter_enabled(cfg) is False
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    assert hunter_enabled(cfg) is True


def test_hunter_results_below_85_are_discarded(cfg, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    low = MockHunter(email="priya@fixturelabs.example", score=84)
    assert (
        hunter_find(
            cfg, domain="fixturelabs.example", company="Fixture Labs",
            role=ROLE_HR, http_get=low,
        )
        is None
    )


def test_hunter_results_at_85_or_above_are_used(cfg, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    good = MockHunter(email="priya@fixturelabs.example", score=85)
    contact = hunter_find(
        cfg, domain="fixturelabs.example", company="Fixture Labs",
        role=ROLE_HR, http_get=good,
    )
    assert contact is not None
    assert contact.email == "priya@fixturelabs.example"
    assert contact.confidence == 85
    assert contact.source == "hunter"


def test_hunter_results_on_another_domain_are_discarded(cfg, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    wrong = MockHunter(email="priya@somewhere-else.example", score=99)
    assert (
        hunter_find(
            cfg, domain="fixturelabs.example", company="Fixture Labs",
            role=ROLE_HR, http_get=wrong,
        )
        is None
    )


def test_hunter_personal_addresses_are_discarded(cfg, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    personal = MockHunter(email="priya@gmail.com", score=99)
    assert (
        hunter_find(
            cfg, domain="gmail.com", company="Fixture Labs",
            role=ROLE_HR, http_get=personal,
        )
        is None
    )


def test_hunter_is_not_called_without_a_key(cfg, monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    spy = MockHunter(email="x@fixture.example", score=99)
    assert hunter_find(
        cfg, domain="fixture.example", company="X", role=ROLE_HR, http_get=spy
    ) is None
    assert spy.calls == []


def test_a_hunter_failure_does_not_raise(cfg, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")

    def boom(url, params):
        raise RuntimeError("network down")

    assert hunter_find(
        cfg, domain="fixture.example", company="X", role=ROLE_HR, http_get=boom
    ) is None


# --------------------------------------------------------- the pipeline


def test_discovery_prefers_your_verified_contacts(cfg, db, page):
    supplied = [
        Contact(
            company="Fixture Robotics",
            email="priya.nair@fixturerobotics.example",
            role=ROLE_HR,
            name="Priya Nair",
            source="contacts_csv",
            verified=True,
            confidence=100,
        )
    ]
    result = discover_contacts(
        cfg, db,
        company="Fixture Robotics",
        website=fixture_url("company_site.html"),
        page=page,
        csv_contacts=supplied,
    )
    hr = result.by_role(ROLE_HR)
    assert len(hr) == 1
    # Your contact wins over the careers@ address on the page.
    assert hr[0].email == "priya.nair@fixturerobotics.example"
    assert hr[0].verified is True


def test_discovery_finds_up_to_three_contacts(cfg, db, page):
    result = discover_contacts(
        cfg, db,
        company="Fixture Robotics",
        description="Apply by writing to careers@fixturerobotics.example",
        website=fixture_url("company_site.html"),
        page=page,
        max_contacts=3,
    )
    assert 1 <= len(result.contacts) <= 3
    assert len({c.role for c in result.contacts}) == len(result.contacts)
    assert all(is_usable_work_email(c.email) for c in result.contacts)


def test_discovery_writes_contacts_to_the_database(cfg, db, page):
    discover_contacts(
        cfg, db,
        company="Fixture Robotics",
        website=fixture_url("company_site.html"),
        page=page,
    )
    rows = db.all_contacts()
    assert rows
    for row in rows:
        assert row["company"]
        assert row["role"] in ("hr", "founder", "cofounder")
        assert row["source"]
        assert row["source_url"] or row["source"] == "contacts_csv"


def test_discovery_records_a_miss_when_nothing_is_found(cfg, db, page):
    """Nothing found means no_email_found, never a guessed address."""
    result = discover_contacts(
        cfg, db,
        company="Fixture Silent",
        website=fixture_url("company_site_no_email.html"),
        page=page,
    )
    assert result.contacts == []
    assert result.misses
    assert all(reason == "no_email_found" for _, reason in result.misses)

    miss = db.contact_miss("Fixture Silent", ROLE_HR)
    assert miss is not None
    assert miss["reason"] == "no_email_found"


def test_discovery_respects_the_blocklist(cfg, db, page):
    result = discover_contacts(
        cfg, db,
        company="Fixture Robotics",
        description="write to careers@fixturerobotics.example",
        website="",
        page=None,
        blocklist={"fixturerobotics.example"},
    )
    assert result.contacts == []
    assert any("blocklisted" in n for n in result.notes)


def test_discovery_never_duplicates_a_role(cfg, db, page):
    supplied = [
        Contact(company="Fixture Robotics", email="a@fixturerobotics.example", role=ROLE_HR),
        Contact(company="Fixture Robotics", email="b@fixturerobotics.example", role=ROLE_HR),
    ]
    result = discover_contacts(
        cfg, db, company="Fixture Robotics", csv_contacts=supplied, page=None
    )
    assert len(result.by_role(ROLE_HR)) == 1


def test_discovery_reuses_contacts_already_in_the_database(cfg, db):
    db.add_contact(
        company="Fixture Labs",
        role=ROLE_HR,
        email="hr@fixturelabs.example",
        name="Known Person",
        source="careers_page",
        source_url="https://fixturelabs.example",
    )
    result = discover_contacts(cfg, db, company="Fixture Labs", page=None)
    assert [c.email for c in result.by_role(ROLE_HR)] == ["hr@fixturelabs.example"]


def test_no_pattern_guessing_anywhere_in_the_module():
    """A guard against someone adding firstname@domain generation later."""
    import inspect

    import contacts as module

    source = inspect.getsource(module)
    for pattern in (
        '"{0}@{1}"',
        "'{0}@{1}'",
        'first + "@"',
        "first_name + '@'",
        '"." + last',
    ):
        assert pattern not in source, "looks like pattern guessing: " + pattern


# ------------------------------------------------ config that must do something


def test_a_recent_miss_stops_the_site_being_scraped_again(cfg, db, page, monkeypatch):
    """retry_misses_after_days must actually prevent a re-scrape."""
    visits: list[str] = []
    import contacts as module

    real_visit = module._visit

    def counting_visit(p, url):
        visits.append(url)
        return real_visit(p, url)

    monkeypatch.setattr(module, "_visit", counting_visit)

    first = discover_contacts(
        cfg, db, company="Fixture Silent",
        website=fixture_url("company_site_no_email.html"), page=page,
    )
    assert first.contacts == []
    assert visits, "the first run should look at the site"
    assert db.contact_miss("Fixture Silent", ROLE_HR) is not None

    visits.clear()
    second = discover_contacts(
        cfg, db, company="Fixture Silent",
        website=fixture_url("company_site_no_email.html"), page=page,
    )
    assert visits == [], "a fresh miss must stop the re-scrape"
    assert any("skipped scraping" in n for n in second.notes)
    # Still reported as a miss, so the caller's counts stay honest.
    assert second.misses


def test_an_expired_miss_allows_another_look(cfg, db, page):
    from contacts import has_fresh_miss

    db.record_contact_miss("Fixture Silent", ROLE_HR, "no_email_found")
    assert has_fresh_miss(db, "Fixture Silent", ROLE_HR, 30) is True

    # Backdate it beyond the window.
    with db.conn:
        db.conn.execute(
            "UPDATE contact_misses SET date_tried='2020-01-01' WHERE company=?",
            ("Fixture Silent",),
        )
    assert has_fresh_miss(db, "Fixture Silent", ROLE_HR, 30) is False


def test_retry_window_of_zero_disables_the_skip(cfg, db):
    from contacts import has_fresh_miss

    db.record_contact_miss("Fixture Silent", ROLE_HR, "no_email_found")
    assert has_fresh_miss(db, "Fixture Silent", ROLE_HR, 0) is False


def test_a_malformed_miss_date_does_not_raise(cfg, db):
    from contacts import has_fresh_miss

    db.record_contact_miss("Fixture Silent", ROLE_HR, "no_email_found")
    with db.conn:
        db.conn.execute("UPDATE contact_misses SET date_tried='not-a-date'")
    assert has_fresh_miss(db, "Fixture Silent", ROLE_HR, 30) is False


def test_follow_contact_page_false_stays_on_the_landing_page(page, cfg):
    """With the flag off, only the landing page is read.

    The careers page has careers@; the homepage footer only has hello@. So the
    flag changes which address is found, and is visible in the source label.
    """
    found_on, _ = hr_from_company_site(
        page, "Fixture Robotics", fixture_url("company_site.html"),
        follow_contact_page=True,
    )
    found_off, _ = hr_from_company_site(
        page, "Fixture Robotics", fixture_url("company_site.html"),
        follow_contact_page=False,
    )
    assert [c.email for c in found_on] == ["careers@fixturerobotics.example"]
    assert found_on[0].source == "careers_page"

    assert [c.email for c in found_off] == ["hello@fixturerobotics.example"]
    assert found_off[0].source == "company_site", (
        "an address from the homepage footer must not be labelled careers_page"
    )
    assert found_off[0].confidence < found_on[0].confidence


def test_the_source_url_points_at_the_page_the_address_was_on(page, cfg):
    found, _ = hr_from_company_site(
        page, "Fixture Robotics", fixture_url("company_site.html")
    )
    assert found[0].source_url.endswith("company_site_careers.html")


def test_follow_contact_page_is_respected_by_discovery(cfg, db, page):
    cfg.outreach.follow_contact_page = False
    result = discover_contacts(
        cfg, db, company="Fixture Robotics",
        website=fixture_url("company_site.html"), page=page,
    )
    hr = result.by_role(ROLE_HR)
    assert hr, "the homepage address is still found"
    assert hr[0].source == "company_site"
    assert all(c.source != "careers_page" for c in result.contacts)
