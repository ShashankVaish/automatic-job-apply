"""Gmail drafts, off by default.

This module can only ever CREATE A DRAFT. It requests the compose scope, which
would technically permit sending, but no function here reaches the send
endpoint and nothing in the project does either - a test asserts it. Everything
lands in your Drafts folder for you to read and send yourself.

Turn it on with `email.gmail_api: true` in config.yaml, after following the
"Gmail drafts (optional)" section of the README.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from config import Config

log = logging.getLogger(__name__)

# Compose = create drafts. The narrower send-only scope is deliberately not
# requested, and a spec test asserts this list never grows.
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

DEFAULT_CREDENTIALS = "./credentials.json"
DEFAULT_TOKEN = "./data/gmail_token.json"


class GmailNotConfigured(RuntimeError):
    """No credentials.json, or the libraries aren't installed."""


def _paths(cfg: Config) -> tuple[Path, Path]:
    import os  # noqa: PLC0415

    creds = os.getenv("GMAIL_CREDENTIALS_PATH", DEFAULT_CREDENTIALS)
    token = os.getenv("GMAIL_TOKEN_PATH", DEFAULT_TOKEN)
    return cfg.abs_path(creds), cfg.abs_path(token)


def get_service(cfg: Config) -> Any:
    """Build an authorised Gmail service.

    The first call opens a browser for Google's consent screen and then caches
    a refresh token, so later runs are silent.
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
        except Exception as exc:
            log.warning("Could not refresh the Gmail token (%s); re-authorising", exc)
            credentials = None

    if not credentials or not credentials.valid:
        if not creds_path.is_file():
            raise GmailNotConfigured(
                "Gmail drafts are enabled but {0} is missing.\n".format(creds_path)
                + "See the 'Gmail drafts (optional)' section of README.md, or set "
                "email.gmail_api: false in config.yaml."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        print()
        print("A browser window will open for Google sign-in.")
        print("This app asks only for permission to CREATE DRAFTS. It never sends.")
        credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        log.info("Gmail token saved to %s", token_path)

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    sender: str = "",
    attachment: Path | str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if sender:
        message["From"] = sender
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


def create_draft(
    cfg: Config,
    *,
    to: str,
    subject: str,
    body: str,
    attachment: Path | str | None = None,
    service: Any | None = None,
) -> str:
    """Create a Gmail draft. Returns its id. NEVER sends.

    `service` is injectable so tests can run against a mock.
    """
    svc = service or get_service(cfg)
    message = build_message(
        to=to,
        subject=subject,
        body=body,
        sender=cfg.profile.email,
        attachment=attachment,
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
