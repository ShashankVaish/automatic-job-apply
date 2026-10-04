"""Gmail API access: sending outreach, checking for replies, spotting bounces.

OAuth only. Your Gmail password is never asked for, never stored, and SMTP with
a password is never used. `credentials.json` comes from the Google Cloud Console
and the refresh token is cached in `token.json` after a one-time browser
consent.

Scopes requested:
  gmail.send      to send your outreach
  gmail.readonly  to search your own mailbox for replies and bounce notices

Sending is deliberately explicit: `send_message` is only ever called from the
sending loop in `outreach.py`, which enforces the queue mode, the daily cap, the
Mon-Fri 09:30-12:30 window, the per-person and per-company limits, the blocklist
and the reply check before each send.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from config import Config

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

DEFAULT_CREDENTIALS = "./credentials.json"
DEFAULT_TOKEN = "./token.json"

# Senders that indicate a delivery failure rather than a human reply.
BOUNCE_SENDERS = (
    "mailer-daemon",
    "postmaster",
    "mail-daemon",
    "no-reply@google.com",
)

BOUNCE_SUBJECT_HINTS = (
    "undelivered mail returned to sender",
    "delivery status notification",
    "address not found",
    "mail delivery failed",
    "delivery incomplete",
    "undeliverable",
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")


class GmailNotConfigured(RuntimeError):
    """No credentials.json, or the Google libraries aren't installed."""


class GmailSendRefused(RuntimeError):
    """A send was attempted without an explicit go-ahead."""


@dataclass
class SentMessage:
    message_id: str
    thread_id: str


def _paths(cfg: Config) -> tuple[Path, Path]:
    creds = os.getenv("GMAIL_CREDENTIALS_PATH", DEFAULT_CREDENTIALS)
    token = os.getenv("GMAIL_TOKEN_PATH", DEFAULT_TOKEN)
    return cfg.abs_path(creds), cfg.abs_path(token)


def get_service(cfg: Config, *, interactive: bool = True) -> Any:
    """Build an authorised Gmail service.

    The first call opens a browser for Google's consent screen, then caches a
    refresh token so later runs are silent. `interactive=False` refuses to open
    a browser, which is what the tests and any unattended run use.
    """
    try:
        from google.auth.transport.requests import Request  # noqa: PLC0415
        from google.oauth2.credentials import Credentials  # noqa: PLC0415
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415
        from googleapiclient.discovery import build  # noqa: PLC0415
    except ImportError as exc:
        raise GmailNotConfigured(
            "Gmail support needs google-api-python-client and "
            "google-auth-oauthlib: pip install -r requirements.txt"
        ) from exc

    creds_path, token_path = _paths(cfg)
    credentials = None

    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:
            log.warning("Stored Gmail token is unusable (%s); re-authorising", exc)
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not refresh the Gmail token (%s); re-authorising", exc)
            credentials = None

    if not credentials or not credentials.valid:
        if not interactive:
            raise GmailNotConfigured(
                "Gmail needs a one-time browser consent, which this run won't do.\n"
                "Run `python main.py --gmail-auth` once, then try again."
            )
        if not creds_path.is_file():
            raise GmailNotConfigured(
                "Gmail is enabled but {0} is missing.\n".format(creds_path)
                + "See the 'Gmail setup' section of README.md."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        print()
        print("A browser window will open for Google sign-in.")
        print("This grants permission to send mail as you, and to read your")
        print("mailbox so replies and bounces can be detected.")
        credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        log.info("Gmail token saved to %s", token_path)

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


# --------------------------------------------------------------- composing


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    sender: str = "",
    attachment: Path | str | None = None,
    thread_message_id: str = "",
) -> EmailMessage:
    """Build the message. `thread_message_id` is the RFC Message-ID of the
    email we're replying to, which keeps a follow-up in the same thread."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if sender:
        message["From"] = sender
    if thread_message_id:
        message["In-Reply-To"] = thread_message_id
        message["References"] = thread_message_id
    message.set_content(body)

    if attachment:
        path = Path(attachment)
        if path.is_file():
            guessed, _ = mimetypes.guess_type(path.name)
            main, _, sub = (guessed or "application/pdf").partition("/")
            message.add_attachment(
                path.read_bytes(),
                maintype=main,
                subtype=sub or "pdf",
                filename=path.name,
            )
        else:
            log.warning("Attachment not found, sending without it: %s", path)
    return message


def encode(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


# ----------------------------------------------------------------- sending


def send_message(
    cfg: Config,
    *,
    to: str,
    subject: str,
    body: str,
    attachment: Path | str | None = None,
    thread_id: str = "",
    thread_message_id: str = "",
    service: Any | None = None,
    confirmed: bool = False,
) -> SentMessage:
    """Send one email as you.

    `confirmed` must be True. It is the single switch that separates "an email
    the user approved" from "an email some code decided to send", and the
    callers in outreach.py set it only after the queue mode, the window, the
    caps, the blocklist and the reply check have all passed.
    """
    if not confirmed:
        raise GmailSendRefused(
            "send_message() requires confirmed=True; nothing sends by accident"
        )
    if not to or "@" not in to:
        raise ValueError("refusing to send to an invalid address: " + repr(to))

    svc = service or get_service(cfg)
    message = build_message(
        to=to,
        subject=subject,
        body=body,
        sender=cfg.profile.email,
        attachment=attachment,
        thread_message_id=thread_message_id,
    )
    payload: dict[str, Any] = {"raw": encode(message)}
    if thread_id:
        payload["threadId"] = thread_id

    sent = svc.users().messages().send(userId="me", body=payload).execute()
    result = SentMessage(
        message_id=str(sent.get("id", "")), thread_id=str(sent.get("threadId", ""))
    )
    log.info("Sent to %s (message %s)", to, result.message_id)
    return result


def create_draft(
    cfg: Config,
    *,
    to: str,
    subject: str,
    body: str,
    attachment: Path | str | None = None,
    service: Any | None = None,
) -> str:
    """Create a Gmail draft without sending. Used by the apply-by-email flow."""
    svc = service or get_service(cfg)
    message = build_message(
        to=to, subject=subject, body=body, sender=cfg.profile.email, attachment=attachment
    )
    created = (
        svc.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": encode(message)}})
        .execute()
    )
    draft_id = str(created.get("id", ""))
    log.info("Gmail draft %s created for %s (not sent)", draft_id, to)
    return draft_id


# -------------------------------------------------------- reading the inbox


def _headers(message: dict[str, Any]) -> dict[str, str]:
    payload = message.get("payload") or {}
    return {
        (h.get("name") or "").lower(): (h.get("value") or "")
        for h in payload.get("headers") or []
    }


def search(
    cfg: Config, query: str, *, service: Any | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Run a Gmail search and return the matching messages with headers."""
    svc = service or get_service(cfg)
    listed = (
        svc.users()
        .messages()
        .list(userId="me", q=query, maxResults=limit)
        .execute()
    )
    out: list[dict[str, Any]] = []
    for stub in listed.get("messages") or []:
        try:
            full = (
                svc.users()
                .messages()
                .get(userId="me", id=stub["id"], format="metadata")
                .execute()
            )
            out.append(full)
        except Exception as exc:
            log.debug("could not read message %s: %s", stub.get("id"), exc)
    return out


def domain_of(address: str) -> str:
    _, email = parseaddr(address or "")
    _, _, domain = (email or "").partition("@")
    return domain.lower().strip()


def find_reply_from_domain(
    cfg: Config,
    domain: str,
    *,
    within_days: int = 90,
    service: Any | None = None,
) -> dict[str, Any] | None:
    """Has anyone at this domain written to you? Returns the first match.

    Bounce notifications are ignored here - they are handled separately, and a
    bounce is not a reply.
    """
    if not domain:
        return None
    query = "from:@{0} newer_than:{1}d -in:chats".format(domain, int(within_days))
    try:
        messages = search(cfg, query, service=service, limit=10)
    except Exception as exc:
        log.warning("Gmail reply search failed for %s: %s", domain, exc)
        return None

    for message in messages:
        headers = _headers(message)
        sender = headers.get("from", "").lower()
        subject = headers.get("subject", "").lower()
        if any(bad in sender for bad in BOUNCE_SENDERS):
            continue
        if any(hint in subject for hint in BOUNCE_SUBJECT_HINTS):
            continue
        return message
    return None


def find_bounced_addresses(
    cfg: Config, *, within_days: int = 30, service: Any | None = None
) -> dict[str, str]:
    """Addresses that bounced, mapped to the reason, read from your mailbox."""
    query = (
        "(from:mailer-daemon OR from:postmaster OR "
        'subject:"Delivery Status Notification" OR subject:"Address not found") '
        "newer_than:{0}d".format(int(within_days))
    )
    try:
        messages = search(cfg, query, service=service, limit=40)
    except Exception as exc:
        log.warning("Gmail bounce search failed: %s", exc)
        return {}

    found: dict[str, str] = {}
    own = (cfg.profile.email or "").lower()
    for message in messages:
        headers = _headers(message)
        subject = headers.get("subject", "")
        haystack = " ".join(
            [subject, headers.get("to", ""), message.get("snippet", "") or ""]
        )
        for address in EMAIL_RE.findall(haystack):
            low = address.lower()
            if low == own or any(bad in low for bad in BOUNCE_SENDERS):
                continue
            found.setdefault(low, subject or "bounced")
    return found
