# organisation-service

GranthanAI service for organisations (colleges and companies): memberships and roles, invites (email, CSV, link, domain auto-join and
join requests), seat enforcement, org policies, the usage report and the admin audit log. Port **8010**.

**Status: Phases 0-10 are built** (see `reports/P*-report.md`, one per phase). What is production-ready and what depends on other
repos is in `reports/P10-report.md` ("Service status") and `docs/PHASES.md` ("Changes needed in other repos").

Docs: `docs/PHASES.md` (plan and contracts), `docs/hld.md`, `docs/lld.md` (the design as built), `docs/runbook.md` (how to operate it),
`docs/alerts.md` (alerts), `docs/launch-checklist.md` (before the first real college pilot).

## Run

Needs Python >= 3.12, [uv](https://docs.astral.sh/uv/), `make`, and the shared dev infra from `../docker-compose.yml`.

```
make infra-up      # shared Postgres, Redis, Kafka (once)
make setup         # copy .env.example to .env, uv sync
make db-create     # create organisation_service_db (safe to run twice)
make db-migrate    # Alembic upgrade to head (0009_domains)
make topics        # create the Kafka topics this service produces and its DLQ (safe to run twice)
make dev           # http://127.0.0.1:8010
```

Check: `GET /live`, `GET /ready` (503 only if Postgres is down; Redis or Kafka down is `DEGRADED`), `GET /metrics`. `X-Correlation-ID` is
accepted and echoed. Always use `127.0.0.1`, never `localhost` (Windows resolves it to IPv6 first).

Dev helpers: `scripts/make_staff_token.py` (a staff JWT), `scripts/seed_dev.py` (a small organisation), `scripts/read_topic.py`,
`scripts/produce_user_events.py` (auth-service style UserRegistered and EmailVerified), `scripts/produce_user_deleted.py`,
`scripts/replay_dlq.py`, `scripts/send_test_email.py` (one real test email through the SMTP provider), `scripts/make_csv_fixture.py` (the 2,000-row college file).

Tests, one file at a time (the owner runs the full suite):

```
make test FILE=tests/unit/test_config.py
# Windows without make: .venv\Scripts\python -m pytest tests\unit\test_config.py -q
```

Load scripts (run in the background against `make dev`; they are not pytest tests): `python -m tests.load.seat_race`,
`python -m tests.load.internal_latency`, `python -m tests.load.csv_under_load`, `python -m tests.load.measure_q1`.

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

- **billing calls us** (bearer `INTERNAL_SERVICE_TOKENS`): `GET /internal/memberships/{user_id}` (alias `/internal/users/{user_id}/membership`), `GET /internal/orgs/{org_id}`, `GET /internal/orgs/{org_id}/members`.
- **We call billing** (bearer `BILLING_SERVICE_TOKEN`): `GET /internal/orgs/{org_id}/usage` (exists); `GET /internal/orgs/{org_id}/subscription` (not built in billing yet: with `SEAT_SOURCE=billing` it logs once and keeps the seats it has).
- **We publish** (outbox): `org.member.joined`, `org.member.left` (always with `org_type`, keyed by `user_id`), `org.policy.changed`, `org.invite.created` (no token, no link), `org.pilot.ending`.
- **We consume**: `user-events` (`UserRegistered`, `EmailVerified`), `user.deleted`, and `subscription.changed` only when `SEAT_SOURCE=billing`.
- **Public API** (`/org/v1`, through api-gateway; the JWT is verified here): me, org details, members, audit, invites (email, CSV, links), domains, join requests, policies, usage report. `GET /org/v1/invites/preview` is the one route without a JWT. **Staff API** `/admin/org/v1`.
- **Errors** are `application/problem+json` with a `code` (one map: `app/core/error_codes.py`); every 4xx and 5xx is logged once with the code, route template, status and correlation id.

### Service tokens

`INTERNAL_SERVICE_TOKENS` is `name:token,name:token`. **billing-service's `ORG_SERVICE_TOKEN` must equal the `billing:` entry here** (dev: `dev-billing-token`; production: 32+ characters).

```
curl http://127.0.0.1:8010/internal/ping -H "Authorization: Bearer <token>"     # {"caller":"billing"}
```

## Settings

Every setting, its default, and whether production needs it set. In production the service **refuses to start** with a development
default for the ones marked "must be set" (`Settings._production_problems`). `.env.example` has a comment for each.

| Setting | Default | Production |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | set to `production` |
| `DEBUG` | `true` | must be `false` |
| `SERVICE_PORT` | `8010` | |
| `DATABASE_URL` | dev Postgres on 127.0.0.1:5433 | must be set (not the dev password) |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `10`, `10` | `(pool + overflow) x WEB_CONCURRENCY x replicas <= max_connections - 20` (lld.md section 10) |
| `WEB_CONCURRENCY` | `2` (Docker image) | uvicorn processes per container |
| `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_RECYCLE_SECONDS`, `DB_PRE_PING` | `10`, `1800`, `false` | |
| `DB_CONNECT_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_MS` | `5`, `3000` | |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | must be set |
| `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_CONSUMER_GROUP`, `KAFKA_DLQ_TOPIC` | `127.0.0.1:9092`, `organisation-service`, `organisation.dlq` | bootstrap must be set |
| `READY_PROBE_TIMEOUT_SECONDS` | `2` | |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | the gateway's address |
| `JWT_SECRET_KEY` | dev key | **must be set** (the same as auth-service) |
| `JWT_ALGORITHM`, `JWT_ISSUER` | `HS256`, `https://auth.granthan.com` | issuer must match auth-service |
| `INTERNAL_SERVICE_TOKENS` | dev tokens | **must be set** (`name:token`, 32+ characters each) |
| `BILLING_BASE_URL` | `http://127.0.0.1:8090` | must be set |
| `BILLING_SERVICE_TOKEN` | dev token | **must be set** (billing's token for us) |
| `WORKERS_ENABLED` | `true` | `true` on exactly the processes that should run the outbox relay, consumer and jobs |
| `OUTBOX_BATCH_SIZE`, `OUTBOX_POLL_SECONDS`, `JOB_POLL_SECONDS`, `CONSUMER_MAX_RETRIES` | `100`, `1`, `2`, `3` | |
| `EMAIL_SENDER` | `console` | **must be `smtp`** (console is development only) |
| `EMAIL_FROM` | `no-reply@granthan.local` | set to your sending address |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_SSL`, `SMTP_STARTTLS`, `SMTP_TIMEOUT_SECONDS` | `''`, `587`, `''`, `''`, `false`, `true`, `10` | `SMTP_HOST` must be set with `smtp` |
| `EMAIL_SENDS_PER_MINUTE` | `60` | match your SMTP provider's limit |
| `APP_BASE_URL` | `http://127.0.0.1:3000` | **must be the public URL** (invite links are built from it) |
| `INVITE_TTL_DAYS`, `LINK_MAX_TTL_DAYS` | `14`, `30` | |
| `INVITE_TOKEN_SECRET` | dev secret | **must be set** (32+ bytes; rotating it kills every open invite and link) |
| `INVITE_EMAILS_VIA` | `local` | `local`, or `notification` once notification-service exists |
| `INVITES_PER_ORG_PER_DAY`, `CSV_ROWS_PER_ORG_PER_DAY` | `200`, `5000` | |
| `CSV_MAX_ROWS`, `CSV_MAX_BYTES`, `MAX_REQUEST_BYTES` | `5000`, `1048576`, `262144` | |
| `PILOT_MAX_MEMBERS`, `PILOT_DAYS` | `25`, `30` | |
| `SEAT_SOURCE` | `staff` | `staff`, or `billing` once billing has the subscription route |
| `DOMAIN_AUTO_JOIN_ENABLED` | `false` | decide (needs verified emails: auth-service `EMAIL_VERIFICATION_REQUIRED=true`) |
| `DNS_NAMESERVERS` | `''` (system resolver) | e.g. `1.1.1.1,8.8.8.8` |
