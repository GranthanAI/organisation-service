# organisation-service

GranthanAI service for organisations: memberships and roles, invites (email, CSV, link, domain), seat enforcement, org policies and the admin audit log. It does not own prices, subscriptions or wallets (billing-service), login (auth-service) or conversations (conversation-service).

Plan: `docs/PHASES.md`. Design: `docs/hld.md`, `docs/lld.md`. Status: Phases 0-4 (skeleton, pure rules, schema, request plumbing, events and workers).

## Run

Needs Python >= 3.12, [uv](https://docs.astral.sh/uv/), `make`, and the shared dev infra from `../docker-compose.yml`.

```
make infra-up      # shared Postgres, Redis, Kafka (once)
make setup         # copy .env.example to .env, uv sync
make db-create     # create organisation_service_db (safe to run twice)
make db-migrate    # Alembic upgrade to head (Phase 2 schema)
make dev           # http://127.0.0.1:8010
```

Check: `GET /live` (process up), `GET /ready` (Postgres, Redis, Kafka; 503 only if Postgres is down, 200 `DEGRADED` if Redis or Kafka is), `GET /metrics`. `X-Correlation-ID` is accepted or generated and echoed back.

Tests, one file at a time (the owner runs the full suite):

```
make test FILE=tests/unit/test_config.py
# Windows without make: .venv\Scripts\python -m pytest tests\unit\test_config.py -q
```

Always use `127.0.0.1`, never `localhost` (Windows resolves it to IPv6 first).

## Ports

| Service | Address |
| --- | --- |
| auth-service | 8001 |
| conversation-service | 8002 |
| billing-service | 8090 (dev) |
| Kafka | 9092 |
| Redis | 6379 |
| Postgres (host port onto `graphgpt-postgres`) | 5433 |
| **organisation-service** | **8010** |

## Contracts in brief

- **billing calls us** (bearer `INTERNAL_SERVICE_TOKENS`): `GET /internal/memberships/{user_id}` (alias `/internal/users/{user_id}/membership`), `GET /internal/orgs/{org_id}`, `GET /internal/orgs/{org_id}/members` (Phase 5).
- **We call billing**: `GET /internal/orgs/{org_id}/usage` (exists); `/subscription` not built yet.
- **We publish** (outbox, relayed by one worker at a time): `org.member.joined`, `org.member.left` (always with `org_type`, keyed by `user_id`), `org.policy.changed`, `org.invite.created`, `org.pilot.ending`; dead letters to `organisation.dlq` (Phase 4).
- **We consume**: `user-events` (`UserRegistered`, `EmailVerified`), `user.deleted`, later `subscription.changed`.
- **Identity**: public routes verify the auth-service JWT (HS256, issuer `https://auth.granthan.com`, revoked `jti`s checked in Redis); staff is the `roles` claim; internal routes use service tokens.

### Service tokens

`INTERNAL_SERVICE_TOKENS` is `name:token,name:token`. **billing-service's `ORG_SERVICE_TOKEN` must equal the `billing:` entry here** (dev: `dev-billing-token`; production: a unique 32+ character token on both sides). Check a token with:

```
curl http://127.0.0.1:8010/internal/ping -H "Authorization: Bearer <token>"     # {"caller":"billing"}
```

Errors are `application/problem+json` with a `code` and the request's `correlation_id`; every public and admin POST needs an `Idempotency-Key` header.

Money is integer paise; times are UTC and come from `app/core/clock.py`; ids are UUIDv7; the schema changes only through Alembic. In production the service refuses to start with a development default.
