"""Sending email. `EmailSender` is the port; `ConsoleEmailSender` (development) writes the message to the log, and
`SmtpEmailSender` delivers over SMTP. `get_email_sender` refuses the console sender outside development and test, like
auth-service does (it would log invite links)."""

import asyncio
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage

from app.core.config import settings
from app.core.logging import logger

CONSOLE_ALLOWED_ENVIRONMENTS = frozenset({"development", "dev", "test", "testing"})
SMTP_SEND_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str | None = None


class EmailSender(ABC):
    @abstractmethod
    async def send(self, message: OutgoingEmail) -> None:
        """Deliver the message or raise. Never swallow a failure: the job retries on exceptions."""


class ConsoleEmailSender(EmailSender):
    """Development only: logs the whole message (the invite link included) instead of sending it."""

    async def send(self, message: OutgoingEmail) -> None:
        logger.info("DEV EMAIL (not sent)", to=message.to, subject=message.subject, body=message.text)


class SmtpEmailSender(EmailSender):
    def __init__(self, timeout: float = SMTP_SEND_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def _send_sync(self, message: OutgoingEmail) -> None:
        mime = MimeMessage()
        mime["From"] = settings.EMAIL_FROM
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        if message.html:
            mime.add_alternative(message.html, subtype="html")
        context = ssl.create_default_context()
        if settings.SMTP_USE_SSL:
            smtp = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=self._timeout, context=context)
        else:
            smtp = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=self._timeout)
        with smtp:
            if settings.SMTP_STARTTLS and not settings.SMTP_USE_SSL:
                smtp.starttls(context=context)
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value())
            smtp.send_message(mime)

    async def send(self, message: OutgoingEmail) -> None:
        """Runs smtplib in a worker thread and gives up after the timeout (a failure raises, so the job retries)."""
        await asyncio.wait_for(asyncio.to_thread(self._send_sync, message), timeout=self._timeout)


def validate_sender_config(sender: str, environment: str, smtp_host: str) -> None:
    sender, env = sender.strip().lower(), environment.strip().lower()
    if sender == "console":
        if env not in CONSOLE_ALLOWED_ENVIRONMENTS:
            raise ValueError(f"EMAIL_SENDER=console is only allowed in development or test (ENVIRONMENT is {env!r}): "
                             "set EMAIL_SENDER=smtp with SMTP_HOST and EMAIL_FROM")
    elif sender == "smtp":
        if not smtp_host.strip():
            raise ValueError("EMAIL_SENDER=smtp requires SMTP_HOST")
    else:
        raise ValueError("EMAIL_SENDER must be 'console' or 'smtp'")


def get_email_sender() -> EmailSender:
    validate_sender_config(settings.EMAIL_SENDER, settings.ENVIRONMENT, settings.SMTP_HOST)
    return SmtpEmailSender() if settings.EMAIL_SENDER.strip().lower() == "smtp" else ConsoleEmailSender()
