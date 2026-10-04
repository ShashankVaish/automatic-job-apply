"""A fake Gmail API service, so tests exercise the real sending code paths
without a consent screen and without a single real email leaving the machine.

It mimics only the shape the project uses:
    service.users().messages().send(userId=..., body={...}).execute()
    service.users().messages().list(userId=..., q=..., maxResults=...).execute()
    service.users().messages().get(userId=..., id=..., format=...).execute()
    service.users().drafts().create(...).execute()
    service.users().getProfile(userId=...).execute()
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SentRecord:
    to: str
    subject: str
    body: str
    raw: str
    thread_id: str
    message_id: str
    has_attachment: bool
    attachment_names: list[str] = field(default_factory=list)


class _Execute:
    def __init__(self, value: Any) -> None:
        self._value = value

    def execute(self) -> Any:
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class MockMessages:
    def __init__(self, service: "MockGmailService") -> None:
        self.service = service

    def send(self, userId: str, body: dict) -> _Execute:
        service = self.service
        if service.fail_next_send:
            service.fail_next_send = False
            return _Execute(RuntimeError("mock Gmail refused the send"))

        raw = base64.urlsafe_b64decode(body["raw"]).decode("utf-8", "replace")
        to = _header(raw, "To")
        subject = _header(raw, "Subject")
        attachments = _attachment_names(raw)

        service.counter += 1
        message_id = "msg-{0}".format(service.counter)
        thread_id = body.get("threadId") or "thread-{0}".format(service.counter)

        service.sent.append(
            SentRecord(
                to=to,
                subject=subject,
                body=raw,
                raw=raw,
                thread_id=thread_id,
                message_id=message_id,
                has_attachment=bool(attachments),
                attachment_names=attachments,
            )
        )
        return _Execute({"id": message_id, "threadId": thread_id})

    def list(self, userId: str, q: str = "", maxResults: int = 10) -> _Execute:
        self.service.searches.append(q)
        matches = self.service.match(q)
        return _Execute({"messages": [{"id": m["id"]} for m in matches[:maxResults]]})

    def get(self, userId: str, id: str, format: str = "metadata") -> _Execute:
        for message in self.service.inbox:
            if message["id"] == id:
                return _Execute(message)
        return _Execute({"id": id, "payload": {"headers": []}, "snippet": ""})


class MockDrafts:
    def __init__(self, service: "MockGmailService") -> None:
        self.service = service

    def create(self, userId: str, body: dict) -> _Execute:
        self.service.counter += 1
        draft_id = "draft-{0}".format(self.service.counter)
        self.service.drafts.append(body)
        return _Execute({"id": draft_id})


class MockUsers:
    def __init__(self, service: "MockGmailService") -> None:
        self.service = service

    def messages(self) -> MockMessages:
        return MockMessages(self.service)

    def drafts(self) -> MockDrafts:
        return MockDrafts(self.service)

    def getProfile(self, userId: str) -> _Execute:
        return _Execute({"emailAddress": self.service.address})


class MockGmailService:
    """Records what was sent and answers searches from a fake inbox."""

    def __init__(self, address: str = "asha.verma@example.com") -> None:
        self.address = address
        self.sent: list[SentRecord] = []
        self.drafts: list[dict] = []
        self.searches: list[str] = []
        self.inbox: list[dict] = []
        self.counter = 0
        self.fail_next_send = False

    def users(self) -> MockUsers:
        return MockUsers(self)

    # ------------------------------------------------------------- fixtures

    def add_inbox_message(
        self,
        *,
        sender: str,
        subject: str = "Re: your application",
        snippet: str = "Thanks for reaching out",
        to: str = "",
    ) -> None:
        self.counter += 1
        self.inbox.append(
            {
                "id": "in-{0}".format(self.counter),
                "snippet": snippet,
                "payload": {
                    "headers": [
                        {"name": "From", "value": sender},
                        {"name": "Subject", "value": subject},
                        {"name": "To", "value": to or self.address},
                    ]
                },
            }
        )

    def add_reply_from(self, domain: str, *, name: str = "Priya Nair") -> None:
        self.add_inbox_message(
            sender="{0} <priya@{1}>".format(name, domain),
            subject="Re: Application",
            snippet="Thanks for applying, can we talk on Friday?",
        )

    def add_bounce_for(self, address: str) -> None:
        self.add_inbox_message(
            sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
            subject="Delivery Status Notification (Failure)",
            snippet="Address not found: {0} - the email account does not exist".format(
                address
            ),
        )

    # --------------------------------------------------------------- search

    def match(self, query: str) -> list[dict]:
        """A deliberately crude stand-in for Gmail's search syntax."""
        q = (query or "").lower()
        wants_bounce = any(
            token in q
            for token in ("mailer-daemon", "postmaster", "delivery status", "address not found")
        )

        from_domain = ""
        for part in q.split():
            if part.startswith("from:@"):
                from_domain = part[len("from:@") :]
                break

        out: list[dict] = []
        for message in self.inbox:
            headers = {
                (h["name"] or "").lower(): (h["value"] or "")
                for h in message["payload"]["headers"]
            }
            sender = headers.get("from", "").lower()
            subject = headers.get("subject", "").lower()
            is_bounce = "mailer-daemon" in sender or "postmaster" in sender or (
                "delivery status" in subject or "address not found" in subject
            )

            if wants_bounce:
                if is_bounce:
                    out.append(message)
                continue
            if from_domain:
                if from_domain in sender and not is_bounce:
                    out.append(message)
                continue
            out.append(message)
        return out


def _header(raw: str, name: str) -> str:
    for line in raw.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
        if not line.strip():
            break
    return ""


def _attachment_names(raw: str) -> list[str]:
    names: list[str] = []
    for line in raw.splitlines():
        low = line.lower()
        if "filename=" in low:
            names.append(line.split("filename=", 1)[1].strip().strip('"'))
    return names


class MockHunter:
    """Stand-in for the Hunter.io HTTP call."""

    def __init__(self, *, email: str = "", score: int = 90, name: str = "Priya Nair") -> None:
        self.email = email
        self.score = score
        self.name = name
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, params: dict) -> dict:
        self.calls.append((url, params))
        if not self.email:
            return {"data": {"emails": []}}
        first, _, last = self.name.partition(" ")
        return {
            "data": {
                "email": self.email,
                "value": self.email,
                "score": self.score,
                "first_name": first,
                "last_name": last,
            }
        }
