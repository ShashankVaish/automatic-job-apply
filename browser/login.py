"""Login state detection and manual-login pauses.

Passwords are never stored, read or typed by this program. When a site looks
logged out we open its login page and wait for you to sign in by hand (email,
Google, OTP, whatever). After that the browser profile keeps the session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SiteAuth:
    """How to tell whether we're signed in to one site."""

    site: str
    login_url: str
    home_url: str
    # Any of these visible => we are logged OUT.
    logged_out_selectors: list[str] = field(default_factory=list)
    # Any of these visible => we are logged IN.
    logged_in_selectors: list[str] = field(default_factory=list)
    # Substrings in the URL that mean we got bounced to a login wall.
    logged_out_url_marks: list[str] = field(default_factory=list)


AUTH: dict[str, SiteAuth] = {
    "linkedin": SiteAuth(
        site="linkedin",
        login_url="https://www.linkedin.com/login",
        home_url="https://www.linkedin.com/feed/",
        logged_out_selectors=[
            "form.login__form",
            "input#session_key",
            "a[href*='/signup']",
        ],
        logged_in_selectors=[
            "img.global-nav__me-photo",
            "button.global-nav__primary-link-me-menu-trigger",
            "div.global-nav__me",
            "input.search-global-typeahead__input",
        ],
        logged_out_url_marks=["/login", "/uas/login", "/authwall", "/checkpoint"],
    ),
    "internshala": SiteAuth(
        site="internshala",
        login_url="https://internshala.com/login/student",
        home_url="https://internshala.com/student/dashboard",
        logged_out_selectors=["#modal_login_form", "a[href*='/login']", "#login"],
        logged_in_selectors=[
            "a[href*='/student/dashboard']",
            "#profile_container",
            "img.profile_pic",
        ],
        logged_out_url_marks=["/login"],
    ),
    "naukri": SiteAuth(
        site="naukri",
        login_url="https://www.naukri.com/nlogin/login",
        home_url="https://www.naukri.com/mnjuser/homepage",
        logged_out_selectors=["#login_Layer", "a[title='Jobseeker Login']"],
        logged_in_selectors=[
            "div.nI-gNb-drawer__icon",
            "a[href*='/mnjuser/profile']",
            "div.view-profile-wrapper",
        ],
        logged_out_url_marks=["/nlogin/login"],
    ),
    "indeed": SiteAuth(
        site="indeed",
        login_url="https://secure.indeed.com/auth",
        home_url="https://www.indeed.com/",
        logged_out_selectors=["a[data-gnav-element-name='SignIn']", "#login-email-input"],
        logged_in_selectors=[
            "[data-gnav-element-name='Profile']",
            "button[aria-label*='Account']",
            "#gnav-main-container [data-gnav-element-name='MyJobs']",
        ],
        logged_out_url_marks=["secure.indeed.com/auth", "/account/login"],
    ),
}

# Signals that we've been rate-limited, challenged or flagged. Any hit means
# "stop touching this site today" (section 9 of the spec).
BLOCK_PHRASES = [
    "unusual activity",
    "suspicious activity",
    "verify you are human",
    "verify you're human",
    "are you a robot",
    "i'm not a robot",
    "complete the captcha",
    "security check",
    "too many requests",
    "rate limit",
    "you have exceeded",
    "temporarily blocked",
    "access denied",
    "let's do a quick security check",
]

BLOCK_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[title*='reCAPTCHA']",
    "iframe[src*='hcaptcha']",
    "div.g-recaptcha",
    "#px-captcha",
    "iframe[src*='challenges.cloudflare.com']",
    "#challenge-running",
]


def _any_visible(page, selectors: list[str], timeout_ms: int = 1200) -> str | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout_ms):
                return sel
        except Exception:
            continue
    return None


def detect_block(page) -> str | None:
    """Return a human-readable reason if this page is a CAPTCHA/rate-limit wall."""
    hit = _any_visible(page, BLOCK_SELECTORS, timeout_ms=700)
    if hit:
        return "challenge element present: " + hit
    try:
        # Only the first chunk of text; these banners are always near the top.
        body = (page.inner_text("body", timeout=3000) or "")[:4000].lower()
    except Exception:
        return None
    for phrase in BLOCK_PHRASES:
        if phrase in body:
            return "page says: " + phrase
    return None


def is_logged_in(page, site: str) -> bool:
    """Best-effort check. Ambiguous pages are treated as logged out."""
    auth = AUTH.get(site)
    if auth is None:
        return True  # career pages and ATS forms need no account

    url = (page.url or "").lower()
    for mark in auth.logged_out_url_marks:
        if mark.lower() in url:
            return False
    if _any_visible(page, auth.logged_in_selectors):
        return True
    if _any_visible(page, auth.logged_out_selectors):
        return False
    # Nothing matched: selectors drift, so don't claim we're in.
    log.debug("Login state for %s is ambiguous at %s", site, page.url)
    return False


def ensure_logged_in(page, site: str, *, prompt_fn=input) -> bool:
    """Make sure we're signed in to `site`, pausing for a manual login if not.

    Returns True once the check passes, False if the user gave up. Never types
    or stores credentials.
    """
    auth = AUTH.get(site)
    if auth is None:
        return True

    try:
        page.goto(auth.home_url, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("Could not open %s: %s", auth.home_url, exc)

    blocked = detect_block(page)
    if blocked:
        log.warning("%s shows a challenge before login (%s)", site, blocked)

    if is_logged_in(page, site):
        log.info("%s: already signed in", site)
        return True

    try:
        page.goto(auth.login_url, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("Could not open the %s login page: %s", site, exc)

    for attempt in range(1, 4):
        print()
        print("=" * 68)
        print("  " + site.upper() + " needs a manual login (attempt {0}/3)".format(attempt))
        print("  A browser window is open at: " + auth.login_url)
        print("  Sign in however you normally do - email, Google, or OTP.")
        print("  This program never sees, types or stores your password.")
        print("=" * 68)
        prompt_fn("  Log in manually (email / Google / OTP), then press Enter. ")

        try:
            page.goto(auth.home_url, wait_until="domcontentloaded")
        except Exception:
            pass
        if is_logged_in(page, site):
            print("  " + site + ": signed in. Session saved for next time.\n")
            log.info("%s: manual login succeeded", site)
            return True
        print("  Still looks signed out. Finish the login in the browser window.")

    log.error("%s: giving up after 3 manual login attempts", site)
    print("  Skipping " + site + " for this run.\n")
    return False
