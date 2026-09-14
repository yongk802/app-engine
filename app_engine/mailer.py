"""Invitation email for a public-origin engine.

Only the owner can cause mail to leave the server (the route sits behind the owner
session and the launcher capability; players have no mail surface at all), and even
the owner is metered: a fixed number of messages per hour and per day, and a few per
recipient per day, so a runaway script or a compromised owner session cannot flood
anyone's inbox or the server's mail reputation. Every attempt is audited.

Delivery is optional. With ``APP_ENGINE_SMTP_URL`` set the engine sends the message
itself; without it the route returns the drafted subject and body plus a ``mailto:``
link, and the owner's own mail program sends it.
"""

from __future__ import annotations

import asyncio
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from urllib.parse import quote, unquote, urlsplit

EMAILS_PER_HOUR = int(os.environ.get("APP_ENGINE_MAIL_PER_HOUR", "20") or 20)
EMAILS_PER_DAY = int(os.environ.get("APP_ENGINE_MAIL_PER_DAY", "100") or 100)
EMAILS_PER_RECIPIENT_PER_DAY = 3
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def clean_address(value: object) -> str | None:
    """One plain address, or None. Display names, angle brackets and anything that could
    smuggle a header (newlines, commas) are refused rather than parsed leniently."""
    if not isinstance(value, str) or len(value) > 254 or "\n" in value or "\r" in value or "," in value:
        return None
    address = value.strip()
    if not _ADDRESS.match(address) or parseaddr(address)[1] != address:
        return None
    return address


def invitation_email(*, server_name: str, origin: str, username: str, link: str, game: str, inviter: str, days: int = 7) -> tuple[str, str]:
    """Subject and plain-text body for a closed-beta invitation. The player already owns the
    game; the server only hosts the tables."""
    subject = f"{inviter} invited you to play {game} online"
    body = f"""Hi,

{inviter} set up a private table server for {game} and invited you to play there. It is a closed beta: you need your own copy of the game already installed; the server does not distribute it.

Your username on {server_name}: {username}

1. Open this link once and choose a password (it expires in {days} days):
   {link}

2. In the game, open Play with a friend. Under Table server pick "{server_name}", enter your username and password, and sign in. From then on your tables, friends and matches are with the other players on that server while the game keeps running on your own computer.

3. To play together: Set up friends and swap friend codes to invite each other straight to a seat, or press Find a random opponent to be paired with whoever is waiting. Tables wait for you, so a match can span days.

If the link has expired or you did not expect this, just ignore it or reply to {inviter}.

Server: {origin}
"""
    return subject, body


def mailto_link(to: str, subject: str, body: str) -> str:
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}"


class MailBudget:
    """Fixed windows per server and per recipient; an exhausted budget is a refusal, never a queue."""

    def __init__(self, per_hour: int = EMAILS_PER_HOUR, per_day: int = EMAILS_PER_DAY, per_recipient: int = EMAILS_PER_RECIPIENT_PER_DAY):
        self.per_hour, self.per_day, self.per_recipient = per_hour, per_day, per_recipient
        self._sent: list[tuple[float, str]] = []

    def reason_to_refuse(self, to: str) -> str | None:
        now = time.time()
        self._sent = [(at, addr) for at, addr in self._sent if now - at < 86400]
        if sum(1 for at, _ in self._sent if now - at < 3600) >= self.per_hour:
            return f"This server sends at most {self.per_hour} invitations an hour. Try later."
        if len(self._sent) >= self.per_day:
            return f"This server sends at most {self.per_day} invitations a day. Try tomorrow."
        if sum(1 for _, addr in self._sent if addr == to.lower()) >= self.per_recipient:
            return f"{to} already received {self.per_recipient} invitations today."
        return None

    def record(self, to: str) -> None:
        self._sent.append((time.time(), to.lower()))


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    starttls: bool
    tls: bool
    sender: str

    @classmethod
    def from_env(cls, url: str | None = None, sender: str | None = None) -> SmtpSettings | None:
        """``smtp://user:pass@host:587`` (STARTTLS) or ``smtps://user:pass@host:465`` (TLS), with
        ``APP_ENGINE_MAIL_FROM`` as the From address."""
        raw = (url if url is not None else os.environ.get("APP_ENGINE_SMTP_URL", "")).strip()
        sender = (sender if sender is not None else os.environ.get("APP_ENGINE_MAIL_FROM", "")).strip()
        if not raw:
            return None
        parts = urlsplit(raw)
        if parts.scheme not in {"smtp", "smtps"} or not parts.hostname:
            raise ValueError("APP_ENGINE_SMTP_URL must look like smtp://user:pass@host:587 or smtps://user:pass@host:465")
        if not clean_address(sender):
            raise ValueError("APP_ENGINE_MAIL_FROM must be a plain email address when APP_ENGINE_SMTP_URL is set")
        tls = parts.scheme == "smtps"
        return cls(host=parts.hostname, port=parts.port or (465 if tls else 587), username=unquote(parts.username or ""),
                   password=unquote(parts.password or ""), starttls=not tls, tls=tls, sender=sender)


class Mailer:
    def __init__(self, settings: SmtpSettings | None, budget: MailBudget | None = None, transport=None):
        self.settings = settings
        self.budget = budget or MailBudget()
        self._transport = transport or self._smtp_send   # tests inject a recorder

    @property
    def configured(self) -> bool:
        return self.settings is not None

    def _smtp_send(self, to: str, subject: str, body: str, server_name: str) -> None:
        assert self.settings is not None
        message = EmailMessage()
        message["From"] = formataddr((server_name, self.settings.sender))
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        if self.settings.tls:
            with smtplib.SMTP_SSL(self.settings.host, self.settings.port, context=context, timeout=20) as smtp:
                if self.settings.username:
                    smtp.login(self.settings.username, self.settings.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.settings.host, self.settings.port, timeout=20) as smtp:
                if self.settings.starttls:
                    smtp.starttls(context=context)
                if self.settings.username:
                    smtp.login(self.settings.username, self.settings.password)
                smtp.send_message(message)

    async def send(self, to: str, subject: str, body: str, server_name: str) -> None:
        """Send within the budget; raises PermissionError with the reason when refused."""
        refusal = self.budget.reason_to_refuse(to)
        if refusal:
            raise PermissionError(refusal)
        await asyncio.to_thread(self._transport, to, subject, body, server_name)
        self.budget.record(to)
