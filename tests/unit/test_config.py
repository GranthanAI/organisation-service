"""The production guard rejects each development default (and never echoes secret values)."""

import pytest
from pydantic import ValidationError

from app.core.config import (
    DEV_BILLING_TOKEN,
    DEV_DATABASE_URL,
    DEV_INTERNAL_TOKENS,
    DEV_JWT_SECRET,
    Settings,
)

GOOD = dict(
    ENVIRONMENT="production",
    DEBUG=False,
    JWT_SECRET_KEY="j" * 40,
    JWT_ISSUER="granthan-auth",
    INTERNAL_SERVICE_TOKENS="billing:" + "b" * 40,
    BILLING_SERVICE_TOKEN="t" * 40,
    DATABASE_URL="postgresql+asyncpg://org_app:Zx9-strong-pw@db.internal:5432/organisation_service_db",
    EMAIL_SENDER="smtp",
    SMTP_HOST="smtp.example.com",
    APP_BASE_URL="https://app.granthan.ai",
    INVITE_TOKEN_SECRET="i" * 40,
)


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_good_production_config_starts():
    assert make().is_production


def test_development_defaults_start_outside_production():
    s = Settings(_env_file=None)
    assert s.SERVICE_PORT == 8010
    assert s.DATABASE_URL == DEV_DATABASE_URL
    assert "127.0.0.1" in s.KAFKA_BOOTSTRAP_SERVERS and "localhost" not in s.REDIS_URL


@pytest.mark.parametrize("env", ["production", "prod", " Production "])
def test_production_aliases_are_guarded(env):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ENVIRONMENT=env)


@pytest.mark.parametrize(
    "overrides, setting",
    [
        ({"JWT_SECRET_KEY": DEV_JWT_SECRET}, "JWT_SECRET_KEY"),
        ({"JWT_SECRET_KEY": ""}, "JWT_SECRET_KEY"),
        ({"JWT_SECRET_KEY": "short"}, "JWT_SECRET_KEY"),
        ({"JWT_SECRET_KEY": "changeme"}, "JWT_SECRET_KEY"),
        ({"JWT_ISSUER": ""}, "JWT_ISSUER"),
        ({"INTERNAL_SERVICE_TOKENS": DEV_INTERNAL_TOKENS}, "INTERNAL_SERVICE_TOKENS"),
        ({"INTERNAL_SERVICE_TOKENS": ""}, "INTERNAL_SERVICE_TOKENS"),
        ({"INTERNAL_SERVICE_TOKENS": "billing:short"}, "INTERNAL_SERVICE_TOKENS"),
        ({"BILLING_SERVICE_TOKEN": DEV_BILLING_TOKEN}, "BILLING_SERVICE_TOKEN"),
        ({"BILLING_SERVICE_TOKEN": ""}, "BILLING_SERVICE_TOKEN"),
        ({"DATABASE_URL": DEV_DATABASE_URL}, "DATABASE_URL"),
        ({"DEBUG": True}, "DEBUG"),
        ({"EMAIL_SENDER": "console"}, "EMAIL_SENDER"),
        ({"SMTP_HOST": ""}, "SMTP_HOST"),
        ({"APP_BASE_URL": "http://127.0.0.1:3000"}, "APP_BASE_URL"),
        ({"INVITE_TOKEN_SECRET": "dev-invite-token-secret-change-me-0123456789abcdef"}, "INVITE_TOKEN_SECRET"),
        ({"INVITE_TOKEN_SECRET": "too-short"}, "INVITE_TOKEN_SECRET"),
        ({"INVITE_TOKEN_SECRET": ""}, "INVITE_TOKEN_SECRET"),
        ({"APP_BASE_URL": "http://localhost:3000"}, "APP_BASE_URL"),
    ],
)
def test_production_guard_rejects_each_dev_default(overrides, setting):
    with pytest.raises(ValidationError) as exc:
        make(**overrides)
    assert setting in str(exc.value)


def test_error_never_contains_secret_values():
    with pytest.raises(ValidationError) as exc:
        make(JWT_SECRET_KEY=DEV_JWT_SECRET, BILLING_SERVICE_TOKEN=DEV_BILLING_TOKEN)
    text = str(exc.value)
    assert DEV_JWT_SECRET not in text and DEV_BILLING_TOKEN not in text


def test_seat_source_must_be_known():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SEAT_SOURCE="nope")


def test_shared_dev_jwt_secret_is_the_default_and_refused_in_production():
    shared = "supersecretjwtkeyforauthservicelocaldvelopment12345"
    assert DEV_JWT_SECRET == shared
    assert Settings(_env_file=None).JWT_SECRET_KEY == shared
    with pytest.raises(ValidationError) as exc:
        make(JWT_SECRET_KEY=shared)
    assert "JWT_SECRET_KEY" in str(exc.value) and shared not in str(exc.value)


def test_the_issuer_defaults_to_what_auth_service_signs_with_and_is_never_empty():
    assert Settings(_env_file=None).JWT_ISSUER == "https://auth.granthan.com"
    for env in ("development", "production"):
        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None, ENVIRONMENT=env, JWT_ISSUER="  ")
        assert "JWT_ISSUER" in str(exc.value)


def test_forwarded_allow_ips_defaults_to_loopback():
    assert Settings(_env_file=None).FORWARDED_ALLOW_IPS == "127.0.0.1"


def test_invite_settings_defaults_and_validation():
    s = Settings(_env_file=None)
    assert s.INVITE_EMAILS_VIA == "local" and len(s.INVITE_TOKEN_SECRET.encode()) >= 32
    assert Settings(_env_file=None, INVITE_EMAILS_VIA="notification").INVITE_EMAILS_VIA == "notification"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, INVITE_EMAILS_VIA="carrier-pigeon")
    with pytest.raises(ValidationError) as exc:
        make(INVITE_TOKEN_SECRET="a" * 31)
    assert "INVITE_TOKEN_SECRET" in str(exc.value) and "a" * 31 not in str(exc.value)
