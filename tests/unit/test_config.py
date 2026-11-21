"""The production guard rejects each development default (and never echoes secret values)."""

import pytest
from pydantic import ValidationError

from app.core.config import (
    DEV_BILLING_TOKEN, DEV_DATABASE_URL, DEV_INTERNAL_TOKENS, DEV_JWT_SECRET, Settings,
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
