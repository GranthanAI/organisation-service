"""Settings (pydantic-settings, `.env`). Every setting has a development default and a line in `.env.example`.

In production the service refuses to start with a development default or an empty secret (`_production_problems`).
Secret VALUES are never put in the error message.
"""

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development defaults that must never reach production.
# Must equal auth-service's dev secret (shared by conversation-service and api-gateway) so dev tokens verify.
DEV_JWT_SECRET = "supersecretjwtkeyforauthservicelocaldvelopment12345"
# What auth-service always signs with (ACCESS_TOKEN_ISSUER) and api-gateway checks.
DEV_JWT_ISSUER = "https://auth.granthan.com"
DEV_INTERNAL_TOKENS = "billing:dev-billing-token,gateway:dev-gateway-token"
DEV_BILLING_TOKEN = "dev-org-service-token"
DEV_DATABASE_URL = "postgresql+asyncpg://postgres:1234@127.0.0.1:5433/organisation_service_db"
DEV_APP_BASE_URL = "http://127.0.0.1:3000"
_KNOWN_WEAK = {"changeme", "secret", "supersecretjwtkeyforauthservicelocaldvelopment12345"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(hide_input_in_errors=True, env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Service ---
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    SERVICE_PORT: int = 8010
    # Confirm the host port against ../docker-compose.yml (5433 maps onto the shared graphgpt-postgres).
    DATABASE_URL: str = DEV_DATABASE_URL
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_CONNECT_TIMEOUT_SECONDS: float = 5.0
    DB_STATEMENT_TIMEOUT_MS: int = 3000
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    KAFKA_BOOTSTRAP_SERVERS: str = "127.0.0.1:9092"
    KAFKA_CONSUMER_GROUP: str = "organisation-service"
    READY_PROBE_TIMEOUT_SECONDS: float = 2.0
    # Proxies (IPs or CIDRs, comma-separated, or *) whose X-Forwarded-For is trusted for per-IP rate limits.
    FORWARDED_ALLOW_IPS: str = "127.0.0.1"

    # --- Identity ---
    JWT_SECRET_KEY: str = DEV_JWT_SECRET
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = DEV_JWT_ISSUER   # always verified; auth-service signs with this value
    INTERNAL_SERVICE_TOKENS: str = DEV_INTERNAL_TOKENS   # name:token,name:token

    # --- Billing ---
    BILLING_BASE_URL: str = "http://127.0.0.1:8090"
    BILLING_SERVICE_TOKEN: str = DEV_BILLING_TOKEN

    # --- Workers ---
    WORKERS_ENABLED: bool = True

    # --- Email ---
    EMAIL_SENDER: str = "console"          # console | smtp (console is refused in production)
    EMAIL_FROM: str = "no-reply@granthan.local"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: SecretStr = SecretStr("")
    SMTP_USE_SSL: bool = False
    SMTP_STARTTLS: bool = True
    SMTP_TIMEOUT_SECONDS: float = 10.0
    APP_BASE_URL: str = DEV_APP_BASE_URL   # for /join/<token> links

    # --- Invites ---
    INVITE_TTL_DAYS: int = 14
    LINK_MAX_TTL_DAYS: int = 30
    INVITES_PER_ORG_PER_DAY: int = 200
    CSV_MAX_ROWS: int = 5000
    CSV_MAX_BYTES: int = 1_048_576

    # --- Pilots and seats ---
    PILOT_MAX_MEMBERS: int = 25
    PILOT_DAYS: int = 30
    SEAT_SOURCE: str = "staff"             # staff | billing

    # --- Domains ---
    DOMAIN_AUTO_JOIN_ENABLED: bool = False

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() in ("production", "prod")

    def _production_problems(self) -> list[str]:
        """Everything wrong with this configuration for production. Empty list = fine. Never includes secret values."""
        problems: list[str] = []

        def secret(name: str, value: str, dev_value: str = "") -> None:
            if not value or value == dev_value or value in _KNOWN_WEAK or len(value) < 32:
                problems.append(f"{name} must be set to a unique secret of at least 32 characters")

        secret("JWT_SECRET_KEY", self.JWT_SECRET_KEY, DEV_JWT_SECRET)
        secret("BILLING_SERVICE_TOKEN", self.BILLING_SERVICE_TOKEN, DEV_BILLING_TOKEN)
        if not self.INTERNAL_SERVICE_TOKENS.strip() or self.INTERNAL_SERVICE_TOKENS == DEV_INTERNAL_TOKENS:
            problems.append("INTERNAL_SERVICE_TOKENS must be set to non-development tokens")
        else:
            for pair in self.INTERNAL_SERVICE_TOKENS.split(","):
                name, _, token = pair.strip().partition(":")
                if not name or len(token) < 32:
                    problems.append("INTERNAL_SERVICE_TOKENS entries must be name:token with tokens of at least 32 characters")
                    break
        if ":1234@" in self.DATABASE_URL:
            problems.append("DATABASE_URL must not use the development password")
        if self.DEBUG:
            problems.append("DEBUG must be false")
        if self.EMAIL_SENDER.strip().lower() == "console":
            problems.append("EMAIL_SENDER must be smtp (console is development only)")
        elif self.EMAIL_SENDER.strip().lower() == "smtp" and not self.SMTP_HOST:
            problems.append("SMTP_HOST must be set when EMAIL_SENDER=smtp")
        if "127.0.0.1" in self.APP_BASE_URL or "localhost" in self.APP_BASE_URL:
            problems.append("APP_BASE_URL must be the public URL (invite links are built from it)")
        return problems

    @model_validator(mode="after")
    def _fail_fast_on_unsafe_production_config(self):
        if self.SEAT_SOURCE not in ("staff", "billing"):
            raise ValueError("SEAT_SOURCE must be staff or billing")
        if not self.JWT_ISSUER.strip():
            raise ValueError("JWT_ISSUER must be set: the issuer is always verified")
        if self.is_production:
            problems = self._production_problems()
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self


settings = Settings()
