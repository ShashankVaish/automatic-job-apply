"""Finding HR, founder and co-founder contacts from legitimate public sources.

Sources, in the order they are tried:

  1. inputs/contacts.csv   addresses you supplied - trusted without further checks
  2. the job post itself    an address written into the description
  3. the careers / contact page on the company site
  4. the about / team page  for founder and co-founder names, and their work
                            email only if the company publishes it
  5. Hunter.io              optional, off by default, and only results with
                            confidence >= 85

Two rules are absolute:

  * **Emails are never guessed from a pattern.** No firstname@company.com, no
    first.last@company.com. A guessed address bounces, gets you marked as spam,
    and burns your real name with that company.
  * **Personal addresses are never used.** gmail.com, outlook.com and the rest
    are rejected even when published.

When nothing valid is found the attempt is recorded as `no_email_found` so the
same company isn't scraped again tomorrow.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from config import Config
from db import Database

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")

ROLE_HR = "hr"
ROLE_FOUNDER = "founder"
ROLE_COFOUNDER = "cofounder"
ROLES = (ROLE_HR, ROLE_FOUNDER, ROLE_COFOUNDER)

# Free/personal mail hosts. A work email is the whole point, so these are out.
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "ymail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "zoho.com", "zohomail.com", "rediffmail.com", "mail.com", "gmx.com",
    "yandex.com", "tutanota.com", "fastmail.com", "hey.com",
}

# Never worth contacting, and in some cases actively harmful to contact.
JUNK_LOCAL_PARTS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "abuse",
    "unsubscribe", "bounce", "mailer-daemon", "privacy", "legal", "dmca",
    "security", "billing", "invoice", "accounts", "payments", "refund",
    "sales", "marketing", "press", "media", "newsletter", "subscribe",
    "webmaster", "admin", "root", "spam", "complaint", "grievance",
)

# Local parts that mark a hiring inbox, best first.
HR_LOCAL_PARTS = (
    "hr", "careers", "career", "jobs", "job", "talent", "recruiting",
    "recruitment", "recruiter", "hiring", "people", "internship",
    "internships", "apply", "resume", "cv",
)

# Weaker, but still a company inbox that may reach a human.
GENERAL_LOCAL_PARTS = ("hello", "contact", "team", "info", "work", "connect")

CAREERS_PATHS = (
    "careers", "career", "jobs", "join-us", "join", "work-with-us",
    "we-are-hiring", "hiring", "contact", "contact-us",
)
TEAM_PATHS = ("about", "about-us", "team", "our-team", "leadership", "people", "founders")

FOUNDER_TITLE_RE = re.compile(
    r"\b(co[\s-]?founder|cofounder|founder|founding (partner|member)|"
    r"chief executive|ceo|cto|coo|cmo|cpo|managing director)\b",
    re.I,
)

CEO_TITLE_RE = re.compile(r"\b(founder|chief executive|ceo|managing director)\b", re.I)

# "Priya Nair" / "Priya S. Nair" - two or three capitalised words.
NAME_RE = re.compile(r"\b([A-Z][a-z]{1,15}(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]{1,20})\b")

CONTACTS_CSV_COLUMNS = ("company", "name", "role", "email", "source_url", "notes")


@dataclass
class Contact:
    company: str
    email: str
    role: str = ROLE_HR
    name: str = ""
    source: str = ""
    source_url: str = ""
    confidence: int | None = None
    verified: bool = False
    note: str = ""

    def label(self) -> str:
        who = self.name or self.role
        return "{0} <{1}> ({2} at {3})".format(
            who, self.email, self.role, self.company or "?"
        )


@dataclass
class DiscoveryResult:
    company: str
    contacts: list[Contact] = field(default_factory=list)
    misses: list[tuple[str, str]] = field(default_factory=list)  # (role, reason)
    notes: list[str] = field(default_factory=list)

    def by_role(self, role: str) -> list[Contact]:
        return [c for c in self.contacts if c.role == role]


# --------------------------------------------------------- address checks


def domain_of(email: str) -> str:
    return (email or "").strip().lower().rpartition("@")[2]


def local_part(email: str) -> str:
    return (email or "").strip().lower().partition("@")[0]


def is_personal_domain(email: str) -> bool:
    return domain_of(email) in PERSONAL_DOMAINS


def is_junk(email: str) -> bool:
    local = local_part(email)
    if not local:
        return True
    return any(junk in local for junk in JUNK_LOCAL_PARTS)


def is_usable_work_email(email: str) -> bool:
    """A company address that a human might read."""
    address = (email or "").strip().lower()
    if not address or address.count("@") != 1:
        return False
    if not EMAIL_RE.fullmatch(address):
        return False
    if is_personal_domain(address):
        return False
    if is_junk(address):
        return False
    domain = domain_of(address)
    if "." not in domain or domain.endswith((".png", ".jpg", ".svg", ".webp", ".gif")):
        return False
    return True


def hr_rank(email: str) -> int:
    """Lower is a better HR inbox. Returns a large number for 'not obviously HR'."""
    local = local_part(email)
    for i, part in enumerate(HR_LOCAL_PARTS):
        if part in local:
            return i
    for i, part in enumerate(GENERAL_LOCAL_PARTS):
        if part in local:
            return len(HR_LOCAL_PARTS) + i
    return 500


def clean_addresses(raw: Iterable[str]) -> list[str]:
    """Normalise, dedupe and drop anything unusable."""
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        address = (item or "").strip().strip(".,;:()<>[]'\"").lower()
        if not address or address in seen:
            continue
        seen.add(address)
        if is_usable_work_email(address):
            out.append(address)
    return out


def addresses_in(text: str) -> list[str]:
    return EMAIL_RE.findall(text or "")


def best_hr_address(candidates: Iterable[str]) -> str:
    usable = clean_addresses(candidates)
    if not usable:
        return ""
    usable.sort(key=lambda a: (hr_rank(a), len(a)))
    return usable[0]


def matches_company_domain(email: str, company_domain: str) -> bool:
    """Is this address on the company's own domain?"""
    if not company_domain:
        return True
    domain = domain_of(email)
    return domain == company_domain or domain.endswith("." + company_domain)


def site_domain(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


# ------------------------------------------------------ 1. inputs/contacts.csv


def load_contacts_csv(path: Path) -> list[Contact]:
    """Contacts you supplied. Treated as verified - you checked them."""
    if not path.is_file():
        return []
    out: list[Contact] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            email = lower.get("email", "").lower()
            if not email or "@" not in email:
                continue
            role = (lower.get("role", "") or ROLE_HR).lower().replace("-", "")
            if role in ("co founder", "co_founder"):
                role = ROLE_COFOUNDER
            if role not in ROLES:
                role = ROLE_HR
            out.append(
                Contact(
                    company=lower.get("company", ""),
                    email=email,
                    role=role,
                    name=lower.get("name", ""),
                    source="contacts_csv",
                    source_url=lower.get("source_url", ""),
                    confidence=100,
                    verified=True,
                    note=lower.get("notes", ""),
                )
            )
    log.info("Loaded %d verified contact(s) from %s", len(out), path)
    return out


def load_blocklist(path: Path) -> set[str]:
    """Addresses and domains you never want contacted."""
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip().lower()
        if not entry or entry.startswith("#"):
            continue
        out.add(entry.lstrip("@"))
    return out


def is_blocked(email: str, blocklist: set[str]) -> bool:
    address = (email or "").strip().lower()
    if not address:
        return True
    if address in blocklist:
        return True
    return domain_of(address) in blocklist


# ------------------------------------------------------- 2. the job post


def contacts_from_job_post(
    company: str, description: str, job_url: str = ""
) -> list[Contact]:
    """An address written into the posting is the most direct HR route."""
    address = best_hr_address(addresses_in(description))
    if not address:
        return []
    return [
        Contact(
            company=company,
            email=address,
            role=ROLE_HR,
            source="job_post",
            source_url=job_url,
            confidence=95,
            note="address published in the job description",
        )
    ]


# ------------------------------------------- 3/4. the company's own website


def _visit(page: Any, url: str) -> str:
    """Open a URL and return its text, or "" if it didn't load."""
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        log.debug("could not open %s: %s", url, exc)
        return ""
    try:
        return page.inner_text("body", timeout=8000) or ""
    except Exception:
        return ""


def _mailto_addresses(page: Any) -> list[str]:
    try:
        html = page.content()
    except Exception:
        return []
    return [m.group(1) for m in re.finditer(r"mailto:([^\"'?>\s]+)", html, flags=re.I)]


def _candidate_pages(page: Any, base_url: str, paths: tuple[str, ...]) -> list[str]:
    """Links on the current page that look like the page we want."""
    found: list[str] = []
    for path in paths:
        for selector in (
            "a[href*='/{0}']".format(path),
            "a[href$='/{0}']".format(path),
            "a[href*='{0}']".format(path),
        ):
            try:
                links = page.locator(selector)
                for i in range(min(links.count(), 3)):
                    href = (links.nth(i).get_attribute("href", timeout=1500) or "").strip()
                    if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
                        continue
                    resolved = urljoin(base_url, href)
                    if resolved not in found:
                        found.append(resolved)
            except Exception:
                continue
    return found[:6]


def hr_from_company_site(
    page: Any, company: str, website: str
) -> tuple[list[Contact], list[str]]:
    """Look for a hiring address on the careers or contact page."""
    notes: list[str] = []
    if not website:
        return [], ["no website known"]

    company_domain = site_domain(website)
    text = _visit(page, website)
    if not text:
        return [], ["company website would not load"]

    pool = addresses_in(text) + _mailto_addresses(page)
    pages = _candidate_pages(page, website, CAREERS_PATHS)

    for url in pages:
        sub_text = _visit(page, url)
        if not sub_text:
            continue
        pool += addresses_in(sub_text) + _mailto_addresses(page)
        notes.append("checked " + url)

    on_domain = [a for a in clean_addresses(pool) if matches_company_domain(a, company_domain)]
    address = best_hr_address(on_domain)
    if not address:
        return [], notes + ["no hiring address published on the site"]

    return [
        Contact(
            company=company,
            email=address,
            role=ROLE_HR,
            source="careers_page",
            source_url=website,
            confidence=90,
            note="published on the company site",
        )
    ], notes


def founders_from_company_site(
    page: Any, company: str, website: str
) -> tuple[list[Contact], list[str]]:
    """Read the about/team page for founder names and published work emails.

    Names are useful on their own: a named founder with no address still lets
    the email be addressed properly if you find the address yourself, and it is
    recorded so you can.
    """
    notes: list[str] = []
    if not website:
        return [], ["no website known"]

    company_domain = site_domain(website)
    text = _visit(page, website)
    pages = _candidate_pages(page, website, TEAM_PATHS)
    if not pages and not text:
        return [], ["company website would not load"]

    blocks: list[tuple[str, list[str]]] = []
    if text:
        blocks.append((text, addresses_in(text) + _mailto_addresses(page)))
    for url in pages:
        sub_text = _visit(page, url)
        if not sub_text:
            continue
        notes.append("checked " + url)
        blocks.append((sub_text, addresses_in(sub_text) + _mailto_addresses(page)))

    out: list[Contact] = []
    used_roles: set[str] = set()

    for block_text, block_addresses in blocks:
        for match in FOUNDER_TITLE_RE.finditer(block_text):
            window = block_text[max(0, match.start() - 160) : match.end() + 160]
            title = match.group(0)
            role = ROLE_FOUNDER if CEO_TITLE_RE.search(title) else ROLE_COFOUNDER
            if role in used_roles:
                continue

            names = NAME_RE.findall(window)
            name = names[0] if names else ""

            nearby = clean_addresses(
                [a for a in addresses_in(window) if matches_company_domain(a, company_domain)]
            )
            address = nearby[0] if nearby else ""

            if not address:
                # A personal address for this founder on the same page is still
                # worth nothing to us - we only use work emails.
                notes.append(
                    "found {0}{1} but no published work email".format(
                        role, " (" + name + ")" if name else ""
                    )
                )
                continue

            used_roles.add(role)
            out.append(
                Contact(
                    company=company,
                    email=address,
                    role=role,
                    name=name,
                    source="team_page",
                    source_url=website,
                    confidence=85,
                    note="{0} listed as {1}".format(name or "contact", title),
                )
            )

    return out, notes


# ---------------------------------------------------------- 5. Hunter.io


def hunter_enabled(cfg: Config) -> bool:
    return bool(cfg.outreach.use_hunter and os.getenv("HUNTER_API_KEY", "").strip())


def hunter_find(
    cfg: Config,
    *,
    domain: str,
    company: str,
    role: str,
    first_name: str = "",
    last_name: str = "",
    http_get: Any = None,
) -> Contact | None:
    """Ask Hunter.io for an address. Only results at confidence >= 85 are used.

    `http_get` is injectable so tests never make a real request.
    """
    api_key = os.getenv("HUNTER_API_KEY", "").strip()
    if not api_key or not domain:
        return None

    minimum = int(cfg.outreach.hunter_min_confidence)

    if first_name and last_name:
        url = "https://api.hunter.io/v2/email-finder"
        params = {
            "domain": domain,
            "first_name": first_name,
            "last_name": last_name,
            "api_key": api_key,
        }
    else:
        url = "https://api.hunter.io/v2/domain-search"
        params = {
            "domain": domain,
            "api_key": api_key,
            "limit": "10",
            "department": "hr" if role == ROLE_HR else "executive",
        }

    try:
        payload = (http_get or _default_get)(url, params)
    except Exception as exc:
        log.warning("Hunter.io request failed for %s: %s", domain, exc)
        return None

    data = (payload or {}).get("data") or {}

    candidates: list[dict[str, Any]] = []
    if "email" in data and data.get("email"):
        candidates.append(data)
    for item in data.get("emails") or []:
        candidates.append(item)

    best: Contact | None = None
    for item in candidates:
        address = (item.get("value") or item.get("email") or "").strip().lower()
        score = item.get("score")
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = 0
        if not address or score < minimum:
            continue
        if not is_usable_work_email(address):
            continue
        if not matches_company_domain(address, domain):
            continue
        name = " ".join(
            part for part in (item.get("first_name") or "", item.get("last_name") or "")
            if part
        ).strip()
        contact = Contact(
            company=company,
            email=address,
            role=role,
            name=name,
            source="hunter",
            source_url="https://hunter.io",
            confidence=score,
            note="Hunter.io confidence {0}".format(score),
        )
        if best is None or (contact.confidence or 0) > (best.confidence or 0):
            best = contact

    if best is None:
        log.info(
            "Hunter.io returned nothing at confidence >= %d for %s", minimum, domain
        )
    return best


def _default_get(url: str, params: dict[str, str]) -> dict[str, Any]:
    import json  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


# ------------------------------------------------------------- the pipeline


def discover_contacts(
    cfg: Config,
    db: Database,
    *,
    company: str,
    description: str = "",
    job_url: str = "",
    website: str = "",
    page: Any = None,
    csv_contacts: list[Contact] | None = None,
    blocklist: set[str] | None = None,
    http_get: Any = None,
    max_contacts: int = 3,
) -> DiscoveryResult:
    """Find up to `max_contacts` contacts for one company: HR, founder, cofounder.

    Every contact found is written to the contacts table. Roles we couldn't fill
    are recorded as `no_email_found` so they aren't retried every run.
    """
    result = DiscoveryResult(company=company)
    blocked = blocklist if blocklist is not None else set()
    supplied = csv_contacts or []
    from db import _norm  # noqa: PLC0415

    target = _norm(company)

    def accept(contact: Contact) -> bool:
        if len(result.contacts) >= max_contacts:
            return False
        if not contact.company:
            contact.company = company
        if not is_usable_work_email(contact.email) and not contact.verified:
            result.notes.append("rejected unusable address " + contact.email)
            return False
        if is_blocked(contact.email, blocked):
            result.notes.append("blocklisted: " + contact.email)
            return False
        if any(c.email == contact.email for c in result.contacts):
            return False
        if any(c.role == contact.role for c in result.contacts):
            return False
        result.contacts.append(contact)
        db.add_contact(
            company=contact.company,
            role=contact.role,
            email=contact.email,
            name=contact.name,
            source=contact.source,
            source_url=contact.source_url,
            confidence=contact.confidence,
            verified=contact.verified,
            note=contact.note,
        )
        log.info("Contact: %s via %s", contact.label(), contact.source)
        return True

    # 1. Your own verified list always wins.
    for contact in supplied:
        if _norm(contact.company) == target or not contact.company:
            accept(contact)

    # Anything already in the contacts table for this company counts too.
    for row in db.contacts_for(company):
        if any(c.email == row["email"] for c in result.contacts):
            continue
        accept(
            Contact(
                company=row["company"],
                email=row["email"],
                role=row["role"],
                name=row["name"] or "",
                source=row["source"] or "db",
                source_url=row["source_url"] or "",
                confidence=row["confidence"],
                verified=bool(row["verified"]),
                note=row["note"] or "",
            )
        )

    needs_hr = not result.by_role(ROLE_HR)

    # 2. The job post.
    if needs_hr and description:
        for contact in contacts_from_job_post(company, description, job_url):
            accept(contact)
        needs_hr = not result.by_role(ROLE_HR)

    # 3/4. The company's own site.
    domain = site_domain(website)
    if page is not None and website:
        if needs_hr:
            found, notes = hr_from_company_site(page, company, website)
            result.notes += notes
            for contact in found:
                accept(contact)

        if len(result.contacts) < max_contacts:
            found, notes = founders_from_company_site(page, company, website)
            result.notes += notes
            for contact in found:
                accept(contact)

    # 5. Hunter.io, last and only if you turned it on.
    if hunter_enabled(cfg) and domain and len(result.contacts) < max_contacts:
        for role in (ROLE_HR, ROLE_FOUNDER):
            if result.by_role(role) or len(result.contacts) >= max_contacts:
                continue
            contact = hunter_find(
                cfg,
                domain=domain,
                company=company,
                role=role,
                http_get=http_get,
            )
            if contact is not None:
                accept(contact)

    for role in ROLES[:max_contacts]:
        if not result.by_role(role):
            reason = "no_email_found"
            result.misses.append((role, reason))
            db.record_contact_miss(company, role, reason)
            log.info("%s: no work email found for %s", company, role)

    return result
