"""Email senders: production refuses console; the console sender logs a DEV EMAIL marker; SMTP runs off-thread with a timeout."""

import asyncio
import time

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.email import sender as sender_module
from app.email.sender import (
    ConsoleEmailSender,
    OutgoingEmail,
    SmtpEmailSender,
    get_email_sender,
    validate_sender_config,
)

MAIL = OutgoingEmail(to="student@amity.edu", subject="Hi", text="Open https://app/join/abc", html="<p>Hi</p>")


@pytest.mark.parametrize("env", ["production", "prod", "staging", "qa"])
def test_production_and_other_non_dev_environments_refuse_console(env):
    with pytest.raises(ValueError, match="console"):
        validate_sender_config("console", env, "")


@pytest.mark.parametrize("env", ["development", "dev", "test", "testing", " Development "])
def test_console_is_allowed_in_development_and_test(env):
    validate_sender_config("console", env, "")


def test_smtp_needs_a_host_and_unknown_senders_are_refused():
    validate_sender_config("smtp", "production", "smtp.example.com")
    with pytest.raises(ValueError, match="SMTP_HOST"):
        validate_sender_config("smtp", "production", "  ")
    with pytest.raises(ValueError):
        validate_sender_config("pigeon", "development", "")


def test_the_settings_guard_makes_startup_fail_for_console_in_production():
    good = dict(
        ENVIRONMENT="production", DEBUG=False, JWT_SECRET_KEY="j" * 40, JWT_ISSUER="https://auth", INTERNAL_SERVICE_TOKENS="billing:" + "b" * 40,
        BILLING_SERVICE_TOKEN="t" * 40, DATABASE_URL="postgresql+asyncpg://u:Zx9strong@db/x", EMAIL_SENDER="smtp",
        SMTP_HOST="smtp.example.com", APP_BASE_URL="https://app.granthan.ai", INVITE_TOKEN_SECRET="i" * 40)
    Settings(_env_file=None, **good)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **{**good, "EMAIL_SENDER": "console"})
    assert "EMAIL_SENDER" in str(exc.value)


def test_get_email_sender_follows_the_settings(monkeypatch):
    monkeypatch.setattr(sender_module.settings, "EMAIL_SENDER", "console")
    monkeypatch.setattr(sender_module.settings, "ENVIRONMENT", "development")
    assert isinstance(get_email_sender(), ConsoleEmailSender)
    monkeypatch.setattr(sender_module.settings, "ENVIRONMENT", "production")
    with pytest.raises(ValueError):
        get_email_sender()                                            # refuses console outside development and test
    monkeypatch.setattr(sender_module.settings, "EMAIL_SENDER", "smtp")
    monkeypatch.setattr(sender_module.settings, "SMTP_HOST", "smtp.example.com")
    assert isinstance(get_email_sender(), SmtpEmailSender)


async def test_the_console_sender_logs_once_with_a_dev_email_marker(caplog):
    await ConsoleEmailSender().send(MAIL)
    records = [r for r in caplog.records if "DEV EMAIL" in r.getMessage()]
    assert len(records) == 1
    text = records[0].getMessage()
    assert "student@amity.edu" in text and "https://app/join/abc" in text and "not sent" in text


async def test_the_smtp_sender_runs_in_a_thread_and_a_slow_server_times_out(monkeypatch):
    sender = SmtpEmailSender(timeout=0.2)
    main_thread = __import__("threading").get_ident()
    seen = {}

    def slow(message):
        seen["thread"] = __import__("threading").get_ident()
        time.sleep(1.0)

    monkeypatch.setattr(sender, "_send_sync", slow)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await sender.send(MAIL)
    assert time.monotonic() - started < 0.9                           # gave up at the timeout, not when the server answered
    assert seen["thread"] != main_thread                              # the blocking call was not on the event loop
    await asyncio.sleep(1.0)                                          # let the worker thread finish before the test ends


async def test_an_smtp_failure_raises_so_the_job_retries(monkeypatch):
    sender = SmtpEmailSender(timeout=2)

    def refuse(message):
        raise ConnectionRefusedError("smtp is down")

    monkeypatch.setattr(sender, "_send_sync", refuse)
    with pytest.raises(ConnectionRefusedError):
        await sender.send(MAIL)


def test_the_smtp_message_has_text_html_from_and_subject(monkeypatch):
    sent = {}

    class FakeSmtp:
        def __init__(self, host, port, timeout=None, context=None):
            sent["host"], sent["port"], sent["timeout"] = host, port, timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, mime):
            sent["mime"] = mime

    cfg = sender_module.settings
    monkeypatch.setattr(sender_module.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(cfg, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(cfg, "SMTP_PORT", 587)
    monkeypatch.setattr(cfg, "SMTP_USE_SSL", False)
    monkeypatch.setattr(cfg, "SMTP_STARTTLS", True)
    monkeypatch.setattr(cfg, "SMTP_USERNAME", "mailer")
    SmtpEmailSender(timeout=7)._send_sync(MAIL)
    mime = sent["mime"]
    assert (sent["host"], sent["port"], sent["timeout"], sent["tls"]) == ("smtp.example.com", 587, 7, True)
    assert sent["login"][0] == "mailer"
    assert mime["To"] == "student@amity.edu" and mime["Subject"] == "Hi" and mime["From"] == cfg.EMAIL_FROM
    parts = {p.get_content_type() for p in mime.walk()}
    assert {"text/plain", "text/html"} <= parts
