# organisation-service: coding phases

This is the build plan for `GranthanAI/organisation-service`, for a Claude Code agent working one phase per session.
The design comes from the **GranthanAI org-service HLD** (Claude Doc, 8 Nov 2026). The contracts are checked against
the `main` branches of `billing-service`, `auth-service`, `conversation-service` and `api-gateway` as of 21 Nov 2026.
Where this file and the HLD disagree, **this file wins**. It changes the folder layout to match auth-service and
conversation-service, and it renames the service to `organisation-service`.

**Release mapping:**
- Phases 0–5 make organisation **pilots** possible and switch on the billing-service code that already exists (billing calls org-service).
- Phases 6–9 let colleges and companies run their own members.
- Phase 10 adds policies, usage reports and the launch checks.

**What organisation-service owns:** organisations, memberships and roles, invites (email, CSV, link,
email domain), seat enforcement, org policies, and the audit log of admin actions.

**What it does not own:**
- prices, subscriptions, invoices and wallets (billing-service);
- login and email verification (auth-service);
- conversations and shared content (conversation-service, file-service).

Org admins never see members' conversations.

---

## Rules for the agent (every phase)

1. **Work only in this repo.** If a phase needs a change in another repo, write it down under "Not done" in the phase report, and do not edit that repo.
2. **Commits.**
   - Commit one sub-step at a time, with the message `P<n><letter>: <what>` (for example `P5b: join transaction`).
   - Never push. The owner pushes.
   - End every commit message with the trailer the session gives you.
3. **Tests.**
   - Run tests **one file at a time** with `uv run python -m pytest tests/<dir>/<file>.py -q`. On Windows use `.venv\Scripts\python -m pytest …`.
   - Do **not** run the full suite. The owner runs it overnight.
   - Do **not** use Testcontainers. Database and Kafka tests use the shared dev services from `../docker-compose.yml` with throwaway names (Postgres database `org_test_<8 hex>`, topic `org_test_<8 hex>.*`). Drop them in teardown, and check afterwards that none are left.
   - No live email, DNS or HTTP calls to other services in tests. Use the fakes in `tests/support/`.
4. **Addresses.** Always `127.0.0.1`, never `localhost`: Windows resolves `localhost` to IPv6 first.
5. **Shared dev ports.**

   | Service | Address |
   | --- | --- |
   | auth-service | 8001 |
   | conversation-service | 8002 |
   | billing-service | 8090 (dev instance) |
   | Kafka | 9092 |
   | Redis | 6379 |
   | **organisation-service** | **8010** |

   Before Phase 0 is done, confirm 8010 is free on the owner's machine.
6. **Data.**
   - Money is `int` paise.
   - Times are timezone-aware UTC. "Now" comes from `app/core/clock.py` (`Clock`, with `FixedClock` for tests), never from `datetime.now()` inside services.
   - New ids are UUIDv7, from `app/utils/ids.py`.
   - Emails and domains are stored lower-cased and trimmed.
7. **Database.**
   - Schema changes go only through Alembic migrations (`make db-migrate`), never at startup.
   - The runtime database role has DML rights only.
8. **Secrets.** Never commit `.env`. Every secret has a placeholder in `.env.example`. In production the service refuses to start with a dev default; copy `_production_problems()` from api-gateway's config.
9. **Phase reports.** End each phase with `reports/P<n>-report.md`, in the same shape as conversation-service's `reports/P6-report.md`:
   - commits;
   - contracts touched;
   - a table of the test files run, with results;
   - the exit check (DONE, or why not);
   - decisions needed;
   - not done.
10. **When stuck,** stop and write the question in the report. Do not guess a contract.

---

## Folder layout (same shape as auth-service and conversation-service)

```
organisation-service/
├── app/
│   ├── main.py                    # FastAPI app + lifespan (starts workers when WORKERS_ENABLED)
│   ├── api/
│   │   ├── router.py              # mounts v1, internal, admin
│   │   ├── deps.py
│   │   ├── v1/                    # public, via api-gateway: /org/v1/*
│   │   │   ├── health.py  me.py  orgs.py  members.py  invites.py  invite_links.py
│   │   │   ├── domains.py  policies.py  usage.py  audit.py
│   │   ├── internal/              # service-to-service: /internal/*
│   │   │   ├── memberships.py  orgs.py
│   │   └── admin/                 # GranthanAI staff: /admin/org/v1/*
│   │       └── orgs.py
│   ├── clients/                   # outbound: billing_client.py, kafka_producer.py, kafka_consumer.py,
│   │                              #           redis_client.py, dns_client.py
│   ├── core/                      # config.py, constants.py, enums.py, exceptions.py, logging.py, clock.py,
│   │                              # public_email_domains.py, plan_features.py
│   ├── db/
│   │   ├── base.py  postgres.py  session.py  errors.py
│   │   └── migrations/  (env.py, versions/)
│   ├── email/                     # sender.py (console | smtp, same rules as auth-service), templates.py
│   ├── events/                    # topics.py, schemas.py, producers.py, consumers.py
│   ├── middleware/                # correlation.py, exception_handler.py, rate_limit.py
│   ├── models/                    # SQLAlchemy models, one file per table group
│   ├── repositories/              # one per aggregate
│   ├── schemas/                   # Pydantic request/response models
│   ├── security/                  # jwt.py, models.py (CurrentUser), dependencies.py, service_tokens.py, tokens.py
│   ├── services/                  # business logic; *_rules.py files are pure (no I/O)
│   ├── telemetry/                 # metrics.py, tracing.py
│   ├── utils/                     # ids.py, pagination.py, csv_safe.py
│   └── workers/                   # outbox, consumer, jobs (see Phase 4)
├── tests/
│   ├── unit/  api/  postgres/  integration/  load/
│   ├── fixtures/                  # sample CSVs, event payloads, billing responses
│   └── support/                   # fakes: FakeEmailSender, FakeDns, FakeBillingClient, FixedClock, db helpers
├── docs/                          # hld.md (export of the HLD), lld.md (written in Phase 0), updates/
├── reports/                       # P<n>-report.md
├── scripts/                       # create_db.py, seed_dev.py, smoke.py
├── alembic.ini  pyproject.toml  uv.lock  Makefile  Dockerfile  docker-compose.yml (service only)
├── .env.example  .gitignore  .gitleaks.toml  .pre-commit-config.yaml
├── README.md  PHASES.md
```

**Layer rules,** checked by `tests/unit/test_layering.py`, which reads imports with `ast`:
- `services/*_rules.py` import only the stdlib and `app/core`.
- `api/` never imports `models/` or `repositories/` directly; it goes through `services/`.
- `repositories/` never import `api/`.

---

## Contracts this service must match

### 1. billing-service calls organisation-service

billing's code is in `src/billing/adapters/orgs.py`. Every call carries `Authorization: Bearer <ORG_SERVICE_TOKEN>`, with a 2 s timeout and one retry. Billing caches answers for 60 s.

| Route | Response |
| --- | --- |
| `GET /internal/memberships/{user_id}` | `{org_id, org_type: "college"\|"company", role, status}`, or 404 when the user is in no org. Only `status: "active"` gives the org plan. |
| `GET /internal/users/{user_id}/membership` | Same handler. This alias exists because `contracts/org-service.md` still names this path. |
| `GET /internal/orgs/{org_id}` | `{org_id, name, type, gstin, billing_email, state_code, member_spend_cap_paise}`. `state_code` is the 2-digit GST code, or null. |
| `GET /internal/orgs/{org_id}/members` | `{user_ids: [...]}`, active members only, in one response with no paging. Billing uses it for the pilot check (at most 25). |

### 2. organisation-service calls billing-service

Calls use the same bearer token, through `clients/billing_client.py`.

| Route | Status |
| --- | --- |
| `GET /internal/orgs/{org_id}/usage?from=YYYY-MM-DD&to=YYYY-MM-DD` | Exists. Returns per member and model group: messages and billable units. |
| `GET /internal/orgs/{org_id}/subscription` | **Not built in billing yet.** Seats come from staff until it exists (Phase 5), then from billing (Phase 10). |

### 3. Kafka topics organisation-service publishes (JSON, through the outbox)

Every event carries `event_id`, `schema_version: "1.0"` and `occurred_at` (ISO UTC, `Z`).

| Topic | Payload | Key | Consumer |
| --- | --- | --- | --- |
| `org.member.joined` | `{org_id, user_id, role, org_type, source}` | `user_id` | billing-service (exists) |
| `org.member.left` | `{org_id, user_id, role, org_type, reason: left\|removed\|user_deleted\|org_closed}` | `user_id` | billing-service (exists) |
| `org.policy.changed` | `{org_id, version, lock_modes, approved_mode_ids, member_spend_cap_paise}` | `org_id` | conversation-service (later) |
| `org.invite.created` | `{org_id, invite_id, email, role, org_name, inviter_name, expires_at}` (**no token and no URL**: the raw token is never stored, so it cannot be in an event; see the token design in lld.md §3d) | `org_id` | notification-service (later) |
| `org.pilot.ending` | `{org_id, ends_at, days_left}` | `org_id` | notification-service (later) |
| `organisation.dlq` | dead letters of the topics this service consumes | n/a | n/a |

Two rules for the member events:
- **Always send `org_type`.** If it's missing, billing defaults to `company`, which gives a college member 60 messages a day instead of 30.
- **Key them by `user_id`,** so one person's join and leave stay in order. The HLD said `org_id`; this file changes that.

### 4. Kafka topics organisation-service consumes

| Topic | Shape (from auth-service) | Used for |
| --- | --- | --- |
| `user-events` | `{event_type, payload, created_at}`. Use `event_type` `UserRegistered` (`payload: {event_id, timestamp, user_id, email, full_name}`) and `EmailVerified` (`payload: {event_id, timestamp, user_id, email}`); ignore all others. | Fills `known_users` |
| `user.deleted` | `{event_type: "user.deleted", schema_version, event_id, occurred_at, user_id}`, key `user_id` | Ends memberships, revokes invites |
| `subscription.changed` | billing's event | Phase 10 only: a hint to re-fetch seats |

### 5. Identity (same pattern as conversation-service)

- **Public routes** verify the Bearer JWT themselves:
  - HS256 with `JWT_SECRET_KEY`, the same secret as auth-service;
  - claims `sub`, `email`, `roles`, `jti`, `exp`, `iat`, `iss`.
- **Staff** is the `roles` claim. `admin` implies `billing_admin` and `support`.
  - `support` gets read-only admin routes.
  - `admin` gets every admin route.
- **Internal routes** take `Authorization: Bearer <token>` matched against `INTERNAL_SERVICE_TOKENS` (`name:token,name:token`), compared in constant time. The caller's name is written to logs and the audit log.

### 6. Plans and seats (from billing's `config/plans.yaml` and the plans sheet)

| Org plan | Seats | Lock modes | Shared modes | Usage reports | Spend cap per member |
| --- | --- | --- | --- | --- | --- |
| Org Free (pilot) | up to 25, for 30 days | no | 3 | no | no |
| `org_pro` | min 50 (college) / 5 (company), as bought | yes | 20 | yes | no |
| `org_max` | min 10 (college) / 5 (company), as bought | yes | 50 | yes | no |
| Org Pay as you Go | any number (no seat cap) | yes | unlimited | yes | yes |

billing's `plans.yaml` has no feature flags yet. Until it does, `app/core/plan_features.py` holds this table, keyed by plan code (`pilot`, `org_pro`, `org_max`, `payg`). It must be the only place this table lives.

### Defaults this file assumes (owner can change before Phase 1)

- **D1.** A user is in at most one active organisation, enforced by a partial unique index.
- **D2.** Joins hard-stop when the org is full. There's no overage.
- **D3.** Owners and admins count as seats.
- **D4.** Only GranthanAI staff create organisations in v1, through the admin routes.
- **D5.** Domain auto-join is off unless `DOMAIN_AUTO_JOIN_ENABLED=true`. It needs verified emails, and auth-service runs with `EMAIL_VERIFICATION_REQUIRED=false` by default.
- **D6.** Org admins see member names, emails and usage counts, never content.

---

## Phase 0: Skeleton and tooling

**Goal:** an empty service that installs, boots, answers health checks and has every config value in place.

- **P0a: Project setup.**
  - `pyproject.toml` (uv, Python ≥ 3.12).
  - Dependencies: `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `alembic`, `pydantic[email]`, `pydantic-settings`, `pyjwt[crypto]`, `redis`, `aiokafka`, `httpx`, `structlog`, `prometheus-client`, `python-multipart`, `dnspython`.
  - Dev dependencies: `pytest`, `pytest-asyncio`, `httpx`, `fakeredis`, `ruff`.
  - `.gitignore`, plus `.gitleaks.toml` and `.pre-commit-config.yaml` copied from conversation-service.
- **P0b: Config, logging and clock.**
  - `app/core/config.py` (pydantic-settings, `.env`) with every setting below, with defaults for dev.
  - `_production_problems()` fails startup when `ENVIRONMENT=production` and any dev default or empty secret is used.
  - `app/core/logging.py`: structlog JSON with `correlation_id`.
  - `app/core/clock.py`.
- **P0c: The app.**
  - `app/main.py` with lifespan and routers.
  - `GET /live` (process up) and `GET /ready`. Postgres, Redis and Kafka are each probed with a timeout (`READY_PROBE_TIMEOUT_SECONDS`) and reported in `details`. Only Postgres is required: it down gives 503; Redis or Kafka down gives 200 with `"status": "DEGRADED"`. Body: `{status: UP|DEGRADED|DOWN, required: ["postgres"], failed: [...], details: {...}}`.
  - `GET /metrics`.
  - Correlation middleware: accept `X-Correlation-ID`, or generate one, and echo it back.
- **P0d: Build and run.**
  - `Dockerfile`: multi-stage, python:3.12-slim, non-root user. The same image runs the API, with workers switched on by `WORKERS_ENABLED`.
  - `docker-compose.yml` for this service only. Shared infra stays in `../docker-compose.yml`.
  - `Makefile` targets: `help`, `setup`, `infra-up`, `infra-down`, `db-create`, `db-migrate`, `dev` (port 8010), `test FILE=…`, `lint`, `clean`.
  - `scripts/create_db.py` creates `organisation_service_db` and is safe to run twice.
- **P0e: Docs.**
  - `.env.example` lists every setting with a comment.
  - `README.md` covers how to run, the ports and the contracts summary.
  - `docs/hld.md`: paste the HLD.
  - `docs/lld.md`: tables, routes and events exactly as this file lists them. Later phases update it.

**Settings (all in `.env.example`):**

| Group | Settings |
| --- | --- |
| Service | `ENVIRONMENT`, `SERVICE_PORT=8010`, `DATABASE_URL=postgresql+asyncpg://postgres:1234@127.0.0.1:5433/organisation_service_db` (confirm the port against `../docker-compose.yml`), `DB_POOL_SIZE`, `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092`, `KAFKA_CONSUMER_GROUP=organisation-service` |
| Identity | `JWT_SECRET_KEY`, `JWT_ALGORITHM=HS256`, `JWT_ISSUER`, `INTERNAL_SERVICE_TOKENS` |
| Billing | `BILLING_BASE_URL=http://127.0.0.1:8090`, `BILLING_SERVICE_TOKEN` |
| Workers | `WORKERS_ENABLED=true` |
| Email | `EMAIL_SENDER=console`, `EMAIL_FROM`, `SMTP_*`, `APP_BASE_URL` (for `/join/<token>` links) |
| Invites | `INVITE_TTL_DAYS=14`, `LINK_MAX_TTL_DAYS=30`, `INVITES_PER_ORG_PER_DAY=200`, `CSV_MAX_ROWS=5000`, `CSV_MAX_BYTES=1048576` |
| Pilots and seats | `PILOT_MAX_MEMBERS=25`, `PILOT_DAYS=30`, `SEAT_SOURCE=staff` (`staff`\|`billing`) |
| Domains | `DOMAIN_AUTO_JOIN_ENABLED=false` |

**Tests:**
- `tests/unit/test_config.py`: the production guard rejects each dev default.
- `tests/api/test_health.py`: `/live` returns 200; `/ready` returns 503 when Postgres is down and 200 `DEGRADED` when only Redis or Kafka is down, and a hanging probe is cut at the timeout, using fake probes.

**Exit check:** `make setup && make db-create && make dev` boots on 8010, `/live` returns 200, `/ready` returns 200 with the shared infra up (503 only when Postgres is down), and the two test files pass.

---

## Phase 1: Rules (pure Python, no I/O)

**Goal:** every membership, seat, role and invite rule, unit-tested before any database exists.

- **P1a: Enums.** `app/core/enums.py`:
  - `OrgType` (`college`, `company`);
  - `OrgStatus` (`pilot`, `active`, `pilot_expired`, `suspended`, `closed`);
  - `Role` (`owner`, `admin`, `member`);
  - `MembershipStatus` (`active`, `left`, `removed`);
  - `JoinSource` (`staff`, `invite`, `csv`, `link`, `domain`);
  - `InviteKind` (`email`, `csv`, `link`);
  - `StaffRole` (`support`, `admin`).
- **P1b: Permissions.** `services/permission_rules.py` holds one table mapping `(Action, Role | StaffRole) -> Allowed | Denied | OwnRoleOnly`, plus `check(action, actor) -> None` that raises `ForbiddenRole`. Actions match the HLD matrix:
  - view org, invite, revoke invite, CSV, links;
  - remove member, change role, transfer ownership, close org;
  - edit details, domains, policies;
  - view usage, view audit;
  - leave;
  - staff create, suspend, extend pilot, manual domain verify, set seats.

  Admins can remove members only, not other admins or the owner.
- **P1c: Seats.** `services/seat_rules.py`: `can_join(org_status, seats_bought | None, active_count, is_pilot) -> Ok | SeatsFull | PilotFull | OrgInactive`. `None` seats means no cap (Pay as you Go). Pilots are capped at `PILOT_MAX_MEMBERS`.
- **P1d: Org lifecycle.** `services/org_lifecycle_rules.py` is the transition table for `OrgStatus`:

  | From | To |
  | --- | --- |
  | `pilot` | `active`, `pilot_expired`, `suspended`, `closed` |
  | `pilot_expired` | `pilot` (extend an expired pilot), `active`, `closed` |
  | `active` | `suspended`, `closed` |
  | `suspended` | `pilot` (reactivate a suspended pilot), `pilot_expired` (its pilot ended while suspended), `active`, `closed` |

  `closed` is final. Any other transition raises `IllegalTransition`.
  Pure helper `reactivation_target(was_pilot, pilot_ends_at, now) -> OrgStatus`: `pilot` if `was_pilot` and `pilot_ends_at > now`, `pilot_expired` if `was_pilot` and it ended, else `active`.
- **P1e: Membership.** `services/membership_rules.py`:
  - last-owner protection: `check_can_end(role, active_owner_count, reason)` with `reason` an `EndReason` (`left`, `removed`, `user_deleted`, `org_closed`). `LastOwner` when `left` or `removed` would leave zero active owners; `user_deleted` and `org_closed` always pass and return `owner_missing=True` when the last owner goes;
  - role change rules;
  - ownership transfer: the target must be an active admin, and the old owner becomes admin.
- **P1f: Email and domain rules.**
  - `services/email_rules.py`: normalise an email; split out its domain; check domain syntax.
  - `core/public_email_domains.py`: a frozen set of public mail domains (gmail.com, googlemail.com, yahoo.com, yahoo.co.in, outlook.com, hotmail.com, live.com, icloud.com, rediffmail.com, proton.me, protonmail.com, zoho.com, aol.com, gmx.com, yandex.com).
- **P1g: Tokens.** `security/tokens.py`:
  - `new_token() -> (raw, sha256_hex)` from 32 random bytes (`secrets.token_urlsafe(32)`);
  - `hash_token(raw)`;
  - a constant-time compare.
- **P1h: Ids.** `utils/ids.py`: a UUIDv7 generator. Python 3.12 has none built in, so write it with a test for time-ordering.

**Tests (all `tests/unit/`):**
- `test_permission_rules.py` walks every cell of the matrix.
- `test_seat_rules.py` covers the boundary (seats − 1, seats, seats + 1), pilot 25/26, and `None` seats.
- `test_org_lifecycle_rules.py` checks every allowed and forbidden transition.
- `test_membership_rules.py` covers last owner, transfer and role changes.
- `test_email_rules.py`.
- `test_tokens.py` checks that hashes are not reversible and differ per token.
- `test_ids.py`.
- `test_layering.py` checks the import rules.

**Exit check:** all P1 test files pass, and the `*_rules.py` files import nothing from `models/`, `repositories/`, `db/` or `api/`.

---

## Phase 2: Database schema, models and repositories

**Goal:** the `organisation_service_db` schema with its guards, plus repositories and one transaction per use case.

- **P2a: Alembic.** `alembic.ini` with `script_location = app/db/migrations` (as conversation-service), and an async `env.py` that reads `DATABASE_URL`.
- **P2b: Migration `0001_core`.** Times are `timestamptz`; emails are `citext` (`CREATE EXTENSION IF NOT EXISTS citext`).

  | Table | Columns and rules |
  | --- | --- |
  | `organisations` | `id`, `name`, `slug` (unique), `type`, `legal_name`, `gstin`, `state_code` (char 2), `billing_email`, `status`, `pilot_ends_at`, `created_by`, `created_at`, `updated_at`, `closed_at` |
  | `memberships` | `id`, `org_id` FK, `user_id`, `role`, `status`, `source`, `invited_by`, `joined_at`, `ended_at`, `end_reason`. **Partial unique index `ux_memberships_one_active_org` on `(user_id) WHERE status = 'active'`** (rule D1). Index `(org_id, status)`. |
  | `seat_state` | `org_id` PK FK, `plan_code` (`pilot`\|`org_pro`\|`org_max`\|`payg`), `variant` (`college`\|`company`), `seats_bought` (int, null = no cap), `status`, `period_end`, `source` (`staff`\|`billing`), `source_ref`, `refreshed_at`. **This is the row locked on every join.** |
  | `known_users` | `user_id` PK, `email`, `full_name`, `email_verified_at`, `deleted_at`, `updated_at`. Index on `email`. |
  | `audit_log` | `id`, `org_id`, `actor_id`, `actor_kind` (`user`\|`staff`\|`service`\|`system`), `action`, `target_type`, `target_id`, `details` (jsonb), `reason`, `correlation_id`, `at`. **Insert-only:** a trigger `forbid_change()` raises on `UPDATE` and `DELETE`. |
  | `outbox` | `id`, `topic`, `key`, `payload` (jsonb), `created_at`, `attempts`, `next_attempt_at`, `published_at`, `last_error`. Index on unpublished rows. |
  | `processed_events` | `consumer`, `event_id`, `processed_at`, with PK `(consumer, event_id)` |
  | `idempotency_keys` | `key`, `actor_id`, `route`, `request_hash`, `status_code`, `response` (jsonb), `created_at`; PK `(actor_id, route, key)`; kept 24 h |
  | `jobs` | `id`, `kind`, `payload`, `run_at`, `attempts`, `locked_until`, `last_error`, `done_at` (for scheduled work, see Phase 4) |

- **P2c: Migration `0002_invites`.**

  | Table | Columns and rules |
  | --- | --- |
  | `invites` | `id`, `org_id`, `kind`, `email` (null for links), `role`, `token_hash` (unique), `max_uses`, `uses`, `batch_id`, `domain_restricted` (bool), `expires_at`, `accepted_at`, `accepted_by`, `revoked_at`, `created_by`, `created_at`. Partial unique index `(org_id, email) WHERE kind <> 'link' AND accepted_at IS NULL AND revoked_at IS NULL`: one open invite per email per org. |
  | `invite_batches` | `id`, `org_id`, `uploaded_by`, `filename`, `rows_total`, `rows_ok`, `rows_failed`, `status` (`validating`\|`queued`\|`sending`\|`done`\|`failed`), `errors` (jsonb, at most 5,000 rows), `created_at`, `finished_at` |

- **P2d: Migration `0003_domains_policies`.**

  | Table | Columns and rules |
  | --- | --- |
  | `org_domains` | `org_id`, `domain`, `verify_token`, `verified_at`, `verified_by`, `verify_method` (`dns`\|`staff`), `auto_join`. Partial unique index `(domain) WHERE verified_at IS NOT NULL`. |
  | `join_requests` | `id`, `org_id`, `user_id`, `domain`, `status` (`pending`\|`approved`\|`declined`\|`expired`), `created_at`, `decided_by`, `decided_at` |
  | `org_policies` | `org_id` PK, `lock_modes`, `approved_mode_ids` (uuid[]), `member_spend_cap_paise` (null = none), `version` (int), `updated_by`, `updated_at` |

- **P2e: Models and repositories.**
  - `models/*.py`: SQLAlchemy 2.0 typed models.
  - `repositories/*_repository.py`: `OrgRepository`, `MembershipRepository` (`count_active`, `get_active_for_user`, `list_page`), `SeatStateRepository` (`lock_for_update`), `InviteRepository`, `DomainRepository`, `PolicyRepository`, `AuditRepository` (insert only), `OutboxRepository`, `KnownUserRepository`, `JobRepository`.
  - `db/session.py`: a `unit_of_work()` async context manager. One transaction per use case; any outbox and audit rows are written in the same transaction.
- **P2f: Errors.** `db/errors.py` maps unique-violation names to domain errors: `ux_memberships_one_active_org` → `AlreadyInOrg`; the invite index → `InviteAlreadyOpen`.

**Tests:** `tests/postgres/` against the shared Postgres. Each test makes `org_test_<hex>`, runs `alembic upgrade head`, and drops the database after.
- `test_migrations.py`: upgrade, then downgrade to base, then upgrade again on an empty database.
- `test_audit_guard.py`: `UPDATE` and `DELETE` on `audit_log` both fail.
- `test_one_active_org.py`: a second active membership for the same user fails with `AlreadyInOrg`; an ended membership plus a new active one is fine.
- `test_repositories.py`: a round trip per repository.
- `test_seat_lock.py`: two connections; the second `lock_for_update` waits until the first commits.

**Exit check:** all P2 test files pass, and no `org_test_*` databases are left behind.

---

## Phase 3: Security and request plumbing

**Goal:** every request is identified, authorised, traced and safe to retry, before any business route exists.

- **P3a: User identity.**
  - `security/jwt.py`: `verify_jwt_token()` copied from conversation-service (signature, `exp`, `iss`).
  - `security/models.py`: `CurrentUser {id, email, roles, staff_role}`. `staff_role` is `admin` when `"admin" in roles`, else `support` when `"support" in roles`, else none.
  - `security/dependencies.py`: `get_current_user`, `require_staff(min_role)`, and `require_org_role(org_id, action)`. The last loads the caller's active membership and calls `permission_rules.check`. It answers 404 when the caller isn't in that org, so org ids can't be probed.
- **P3b: Service identity.** `security/service_tokens.py` parses `INTERNAL_SERVICE_TOKENS` and provides `require_service()` for `/internal/*`, which resolves the caller's name. A missing or wrong token gets 401 and writes nothing.
- **P3c: Errors.** `middleware/exception_handler.py` returns `application/problem+json` with `{type, title, status, code, detail, correlation_id}` for every domain error. The codes:

  | Code | HTTP |
  | --- | --- |
  | `SEATS_FULL`, `PILOT_FULL`, `ALREADY_IN_ORG`, `LAST_OWNER`, `ORG_INACTIVE`, `INVITE_ALREADY_OPEN`, `DOMAIN_TAKEN` | 409 |
  | `INVITE_INVALID` | 404 |
  | `INVITE_EXPIRED` | 410 |
  | `INVITE_EMAIL_MISMATCH`, `EMAIL_NOT_VERIFIED`, `FEATURE_NOT_IN_PLAN`, `FORBIDDEN_ROLE` | 403 |
  | `VERSION_CONFLICT` | 412 |
  | `PUBLIC_EMAIL_DOMAIN`, `CSV_INVALID`, `VALIDATION_ERROR` | 422 |
  | `RATE_LIMITED` | 429 |
  | `UPSTREAM_UNAVAILABLE` | 503 |

  An unknown exception becomes a 500 with no internals in the body.
- **P3d: Idempotency.** `services/idempotency_service.py` reads the `Idempotency-Key` header on every public and admin `POST`:
  - same key and same body: replay the stored response;
  - same key with a different body: 422 `IDEMPOTENCY_MISMATCH`.

  Keys are scoped to `(actor_id, route)` and kept for 24 h.
- **P3e: Rate limits.** `middleware/rate_limit.py` uses Redis fixed-window counters for invite creation, CSV upload, link accept and preview. If Redis is down, invite creation and CSV upload fail closed and everything else passes. Each 429 carries `Retry-After`.
- **P3f: Audit.** `services/audit_service.py`: `record(uow, actor, action, target, details, reason)` stamps the correlation id. Every write use case from Phase 5 on calls it.

**Tests:**
- `tests/unit/test_jwt.py`: expired, wrong secret, wrong issuer, missing `sub`.
- `tests/unit/test_staff_roles.py`.
- `tests/api/test_service_tokens.py`: none, wrong and right token; the right one is compared in constant time (`hmac.compare_digest`).
- `tests/api/test_problem_json.py`: one route per error code.
- `tests/unit/test_idempotency.py`.
- `tests/unit/test_rate_limit.py` with fakeredis.

**Exit check:** all P3 test files pass, and a request to a dummy `/internal/ping` with no token returns 401 and logs nothing secret.

---

## Phase 4: Events, workers and the job runner

**Goal:** reliable Kafka out (outbox) and in (deduplicated consumers), plus a scheduler for timed jobs.

- **P4a: Event schemas and producer.**
  - `events/topics.py` lists the topics from Contracts §3–4.
  - `events/schemas.py` holds Pydantic models for every produced and consumed message. Consumed models use `extra="ignore"`.
  - `clients/kafka_producer.py` wraps aiokafka with `acks=all` and idempotence on.
- **P4b: Outbox worker.** `events/producers.py` provides `enqueue(uow, topic, key, payload)`, which only writes an outbox row. `workers/outbox_worker.py`:
  - claims rows with `FOR UPDATE SKIP LOCKED` in batches of 100;
  - publishes, then sets `published_at` only after the broker acks;
  - backs off exponentially, up to 10 minutes;
  - exposes metrics: `org_outbox_pending`, `org_outbox_publish_failures_total`.
- **P4c: Consumers.** `events/consumers.py` and `workers/consumer_worker.py` subscribe to `user-events` and `user.deleted`:
  - deduplicate on `processed_events (consumer, event_id)`;
  - retry 3 times;
  - then send the message to `organisation.dlq` with the error, and commit the offset.

  Handlers:
  - `UserRegistered` upserts `known_users` (email, name).
  - `EmailVerified` sets `email_verified_at`. Phase 9 hooks domain auto-join here.
  - `user.deleted` sets `known_users.deleted_at`; ends every active membership with `end_reason='user_deleted'`, an audit row and `org.member.left`; and revokes open invites to that email. If this leaves an org with no owner, it writes audit action `org.owner_missing` for staff to fix and does not block.
- **P4d: Job runner.** `workers/job_runner.py` claims due `jobs` rows with `SKIP LOCKED`, runs the handler registered for the `kind`, retries with backoff, and marks `done_at`. `services/job_service.py`: `schedule(uow, kind, run_at, payload)` and `schedule_recurring(kind, every)`, idempotent by kind. Handlers are added in the phases that need them.
- **P4e: Startup.** `main.py` starts the outbox worker, consumer worker and job runner only when `WORKERS_ENABLED=true`, and stops them cleanly on shutdown.

**Tests:**
- `tests/unit/test_event_schemas.py`: the consumed examples in `tests/fixtures/` are real payloads copied from auth-service code.
- `tests/postgres/test_outbox_worker.py`: publish succeeds; publish fails then retries; two workers never publish the same row twice. Uses a fake producer.
- `tests/postgres/test_consumers.py`: duplicate `event_id` processed once; `user.deleted` ends membership, emits `org.member.left` and revokes invites; a poison message goes to the DLQ.
- `tests/integration/test_kafka_roundtrip.py`: real Kafka with `org_test_<hex>` topics; outbox to broker to a test consumer.
- `tests/postgres/test_job_runner.py`.

**Exit check:** all P4 test files pass. With the shared Kafka, a `user.deleted` message produced by hand ends a seeded membership, and `org.member.left` appears on the topic.

---

## Phase 5: Billing contract, the join transaction and staff-run organisations

**Goal:** billing-service's existing org code works against a real organisation-service, and staff can run a pilot end to end.

- **P5a: Seat state, staff-sourced.** `services/seat_service.py`: `set_from_staff(org_id, plan_code, variant, seats_bought, period_end)` writes `seat_state` with `source='staff'` and an audit row. `SEAT_SOURCE=billing` is reserved for Phase 10.
- **P5b: The join transaction.** `services/join_service.py`: `join(uow, org_id, user, source, role, invite=None)`. This is the only code path that creates an active membership. In one transaction:
  1. `SELECT … FROM seat_state WHERE org_id = :id FOR UPDATE`.
  2. Load the org status and count active members.
  3. Apply `seat_rules.can_join` (`ORG_INACTIVE`, `SEATS_FULL`, `PILOT_FULL`).
  4. Insert the membership. A unique violation means `ALREADY_IN_ORG`.
  5. If an invite was used, increment `uses` and set `accepted_*`.
  6. Write the audit row `member.joined`.
  7. Enqueue `org.member.joined` with `org_type` and `source`.
- **P5c: Leave and remove.** `services/membership_service.py`: `end(uow, org_id, user_id, reason, actor)` sets `status` and `ended_at`, writes audit, and enqueues `org.member.left`, applying `membership_rules` (last owner).
- **P5d: Internal routes.** `api/internal/memberships.py` and `api/internal/orgs.py` implement the three billing routes plus the alias, exactly as in Contracts §1.
  - `member_spend_cap_paise` comes from `org_policies`, or null.
  - `/members` returns at most 10,000 ids. Above that it returns 422 and logs, because billing only uses it for pilots.
- **P5e: Admin routes.** `api/admin/orgs.py` (`/admin/org/v1`):

  | Route | What it does |
  | --- | --- |
  | `POST /orgs` | Takes `{name, type, legal_name?, gstin?, state_code?, billing_email, owner_email, pilot: bool}`. Creates the org in `pilot` status (with `pilot_ends_at = now + PILOT_DAYS` and `seat_state` = pilot, 25 seats), or in `active` status. If `owner_email` is a known user, the owner joins at once with source `staff`; otherwise an owner invite is created (sent from Phase 7; until then the invite row exists and staff use `POST /orgs/{id}/members` after signup). |
  | `GET /orgs?q=&status=&cursor=` | Search organisations |
  | `GET /orgs/{id}` | One organisation |
  | `POST /orgs/{id}/members` | `{user_id, role}`: add directly |
  | `POST /orgs/{id}/seats` | `{plan_code, variant, seats_bought, period_end}`: staff-sourced seats |
  | `POST /orgs/{id}/suspend`, `/reactivate`, `/close`, `/extend-pilot` | `{days ≤ 30}` for extend-pilot. Close ends every membership with `reason='org_closed'`. |

  - Every write needs `reason` (at least 5 characters).
  - Writes need staff role `admin`; reads also allow `support`.
- **P5f: Pilot jobs.** Job `pilot.remind` emits `org.pilot.ending` at 7 days and 1 day before the end. Job `pilot.expire` moves status to `pilot_expired` at `pilot_ends_at`. Members stay members, but `join` refuses new ones.

**Tests:**
- `tests/postgres/test_join_service.py`: every refusal code; joined event payload; audit row written.
- `tests/postgres/test_seat_race.py`: 20 concurrent joins into 5 free seats (asyncio, separate connections) admit exactly 5.
- `tests/api/test_internal_routes.py`: the shapes match Contracts §1 field for field, including the 404 and the alias.
- `tests/api/test_admin_orgs.py`: pilot creation, the 26th member refused with `PILOT_FULL`, close ends members and emits `left` events, `support` cannot write.
- `tests/postgres/test_pilot_jobs.py` with `FixedClock`.

**Exit check (cross-service, owner may need to start billing):**
1. Run billing-service's dev instance with `ORG_SERVICE_URL=http://127.0.0.1:8010` and `ORG_SERVICE_TOKEN`.
2. Through `/admin/org/v1`, create a college pilot and add a test user.
3. billing's `GET /internal/memberships/{user_id}` call (seen in organisation-service logs) returns the org.
4. billing consumes `org.member.joined` (seen in billing logs), and the user's entitlement shows the org plan. Record what was seen in the report.

If billing can't be started, run `tests/api/test_internal_routes.py` against billing's `HttpOrgDirectory` code copied into `tests/support/`, and mark the cross-service part not done.

---

## Phase 6: Members and organisation API for org owners and admins

**Goal:** an org owner or admin can see and manage their organisation from the app.

- **P6a: Who am I.** `GET /org/v1/me` returns `{org: {id, name, type, status, pilot_ends_at}, role, seats: {used, bought | null, plan_code}, features}` for the caller's active org, or 404. `features` comes from `core/plan_features.py`.
- **P6b: Organisation details.**
  - `GET /org/v1/orgs/{id}`: any member.
  - `PATCH /org/v1/orgs/{id}`: owner or admin. Accepts `name`, `legal_name`, `gstin` (15-character format check), `state_code` (one of the 2-digit GST codes; keep the list in `core/constants.py`), `billing_email`. Audited.
- **P6c: Members.**
  - `GET /org/v1/orgs/{id}/members?cursor=&q=&role=&status=active`: owner or admin. Keyset pagination on `(joined_at, id)`. `q` matches name or email from `known_users`. Each row: `{user_id, name, email, role, source, joined_at}`.
  - `PATCH /org/v1/orgs/{id}/members/{user_id}` `{role}`: owner only for admin changes.
  - `DELETE /org/v1/orgs/{id}/members/{user_id}`: per the matrix, through `membership_service.end(reason='removed')`.
  - `POST /org/v1/orgs/{id}/leave`: through `end(reason='left')`; `LAST_OWNER` when it applies.
  - `POST /org/v1/orgs/{id}/transfer-ownership` `{to_user_id}`.
- **P6d: Audit log.** `GET /org/v1/orgs/{id}/audit?cursor=&action=`: owner or admin; newest first.

**Tests:**
- `tests/api/test_me.py`.
- `tests/api/test_org_details.py` covers GSTIN and state-code validation.
- `tests/api/test_members.py`: paging is stable while members join; admin cannot remove admin; last owner can't leave; transfer; a non-member gets 404, not 403.
- `tests/api/test_audit_api.py`.

**Exit check:** all P6 test files pass. A seeded org of 120 members pages correctly at a page size of 50, and every write in this phase left one audit row.

---

## Phase 7: Email invites

**Goal:** an admin invites people by email; they sign up (or sign in) and land in the organisation.

- **P7a: Creating invites.**
  - `POST /org/v1/orgs/{id}/invites`: owner or admin; `{emails: [..≤50], role}`.
  - For each email it skips anyone already a member, refuses an open duplicate (`INVITE_ALREADY_OPEN`), and creates the invite with a token hash, `expires_at = now + INVITE_TTL_DAYS`, and an `org.invite.created` event (no token, no URL). The raw token is never stored: it is `base64url(HMAC-SHA256(INVITE_TOKEN_SECRET, "invite:<id>:<token_version>"))`, recomputed at send time by the `invite.send` job; only its SHA-256 is saved. Migration `0007_invite_tokens` adds `token_version`, `resend_count`, `last_sent_at`, `purged_at`.
  - The raw token is never stored or logged.
  - Daily limit `INVITES_PER_ORG_PER_DAY`. Free seats are checked as a warning only; acceptance enforces them.
- **P7b: Sending email (until notification-service exists).** `email/sender.py` has `console` and `smtp` senders, with the same production refusal of `console` as auth-service. `email/templates.py` has the invite email (plain text and HTML, with org name, inviter name, link and expiry). The in-process job `invite.send {invite_id, token_version}` sends it (stale versions are skipped). It is a job, not a consumer, so the link can be derived at send time. Turn it off with `INVITE_EMAILS_VIA=notification` once notification-service exists.
- **P7c: Preview.** `GET /org/v1/invites/preview?token=` needs **no JWT**. It returns `{org_name, org_type, inviter_name, email_hint: "s***@amity.edu", expires_at}`, or `INVITE_INVALID`/`INVITE_EXPIRED`. Rate-limited per IP.
- **P7d: Accepting.** `POST /org/v1/invites/accept` `{token}` with a JWT:
  - for email and CSV invites, the token email must equal the JWT `email`, else `INVITE_EMAIL_MISMATCH`;
  - then `join_service.join(source='invite')`.
- **P7e: Managing invites.**
  - `GET /org/v1/orgs/{id}/invites?status=open|accepted|expired|revoked`.
  - `POST …/invites/{invite_id}/resend` issues a new token, so the old link dies, and enqueues a new event. At most 3 resends.
  - `DELETE …/invites/{invite_id}` revokes.
- **P7f: Expiry.** Job `invites.expire`, daily, marks expired invites. A 90-day purge job nulls `email` on invites that were never accepted, so unaccepted students' emails aren't kept.

**Tests:**
- `tests/postgres/test_invite_service.py`: token stored only as a hash; duplicate open invite refused; resend kills the old token.
- `tests/api/test_invite_flow.py`: invite, then preview without a JWT, then accept with the right email, then a member; wrong email gets 403; expired gets 410; a full org gets `SEATS_FULL` and the invite stays open.
- `tests/unit/test_invite_templates.py`.
- `tests/unit/test_email_sender.py`: production refuses `console`.

**Exit check:** all P7 test files pass. With the console sender in dev, the full invite → register on auth-service (8001) → accept flow works by hand, and billing shows the new member on the org plan. Write the steps and results in the report.

---

## Phase 8: CSV bulk invites and invite links

**Goal:** a college uploads its student list, or shares one link, instead of typing emails.

- **P8a: Upload and validation.** `POST /org/v1/orgs/{id}/invites/csv` takes multipart `file`, with optional `role=member`.
  - Limits: `CSV_MAX_BYTES` and `CSV_MAX_ROWS`; UTF-8 with or without BOM; header row required with column `email` and optional `name`, `role`.
  - Validation runs at once, before anything is sent. Each failing row is reported with its row number and reason:
    - `invalid_email`;
    - `duplicate_in_file`;
    - `already_member`;
    - `in_another_org`, using `known_users` and active memberships, without saying which org;
    - `open_invite_exists`;
    - `role_not_allowed`;
    - `over_seats`, for rows beyond free seats.
  - The response is `{batch_id, rows_total, rows_ok, rows_failed, errors_preview: [first 20]}`.
- **P8b: Sending in the background.** Job `csv.send` creates invites (`kind='csv'`, `batch_id`) in chunks of 200, one transaction per chunk, with one `org.invite.created` per invite. It updates the batch counters and can resume if the process restarts.
- **P8c: Batch status.**
  - `GET /org/v1/invite-batches/{batch_id}`: progress.
  - `GET /org/v1/invite-batches/{batch_id}/errors.csv`: the error report. Every cell goes through `utils/csv_safe.py`, which prefixes `'` to cells starting with `=`, `+`, `-`, `@`, tab or CR.
- **P8d: Invite links.**
  - `POST /org/v1/orgs/{id}/invite-links` `{max_uses (1–5000), expires_in_days (≤ LINK_MAX_TTL_DAYS), domain_restricted: bool}` returns the URL once. The role is always `member`.
  - `GET …/invite-links` lists links without tokens.
  - `DELETE …/invite-links/{id}` revokes.
- **P8e: Accepting a link.** It uses the same accept route. For a link, the email isn't bound; if `domain_restricted`, the JWT email's domain must be one of the org's verified domains. `uses` is incremented inside the join transaction, and a link past `max_uses` gets `INVITE_INVALID`.

**Tests:**
- `tests/unit/test_csv_parse.py`: BOM, CRLF, a missing `email` column, 5,001 rows, the formula-injection cells.
- `tests/postgres/test_csv_batch.py`: a 2,000-row file with 50 bad rows produces 1,950 invites and 50 errors; a restart mid-batch doesn't duplicate.
- `tests/api/test_invite_links.py`: `max_uses` is honoured under 10 concurrent accepts; revoke; domain restriction.

**Exit check:** all P8 test files pass. `tests/fixtures/college_2000.csv` (generated by `scripts/make_csv_fixture.py`) uploads in under 5 s on the owner's machine, and the error CSV opens in a spreadsheet with no formulas run.

---

## Phase 9: Email domains and auto-join

**Goal:** a company or college verifies its email domain, so staff with that domain can join without an invite.

- **P9a: Adding a domain.** `POST /org/v1/orgs/{id}/domains` `{domain}`:
  - refuses public mail domains (`PUBLIC_EMAIL_DOMAIN`), and domains already verified by another org (`DOMAIN_TAKEN`);
  - creates `verify_token`, and returns the DNS TXT record to add: `granthan-verify=<token>` at the domain root.
- **P9b: Verifying.**
  - `POST …/domains/{domain}/verify` checks once, synchronously.
  - Job `domains.verify` re-checks unverified domains hourly for 7 days.
  - `clients/dns_client.py` (dnspython, 3 s timeout) is behind a port, faked in tests.
  - Staff can call `POST /admin/org/v1/orgs/{id}/domains/{domain}/manual-verify` `{reason}`, for colleges whose IT is slow.
  - Verification sets `verified_at`, `verify_method` and audit. A domain loses verification after 3 failed re-checks on the 30-day schedule.
- **P9c: Auto-join.** `PATCH …/domains/{domain}` `{auto_join: bool}`. On the `EmailVerified` event (the Phase 4 handler), when `DOMAIN_AUTO_JOIN_ENABLED=true`, the email's domain is verified with `auto_join`, and the user has no active org:
  - `join(source='domain', role='member')`;
  - on `SEATS_FULL`, a `join_requests` row instead.

  Never auto-join from the unverified `UserRegistered` event.
- **P9d: Join requests.**
  - `GET /org/v1/orgs/{id}/join-requests`.
  - `POST …/join-requests/{id}/approve` goes through the join transaction.
  - `POST …/join-requests/{id}/decline`.
  - Requests expire after 30 days.
- **P9e: Listing and removing.** `GET …/domains`, and `DELETE …/domains/{domain}`. Removing a domain doesn't remove members.

**Tests:**
- `tests/unit/test_domain_rules.py`.
- `tests/postgres/test_domain_verify.py`: TXT found, not found, timeout; staff manual verify; one org per domain.
- `tests/postgres/test_auto_join.py`: verified domain with a free seat joins; full org makes a join request; flag off does nothing; `UserRegistered` alone does nothing; a user already in an org is skipped.
- `tests/api/test_domains_api.py`: gmail.com refused.

**Exit check:** all P9 test files pass. A seeded `EmailVerified` event for `x@<verified-domain>` makes an active member and one `org.member.joined` event.

---

## Phase 10: Policies, usage reports, billing seats and launch checks

**Goal:** paid-plan features work, seats come from billing, and the service is ready for a first college pilot.

- **P10a: Policies.**
  - `GET` and `PUT /org/v1/orgs/{id}/policies` with `If-Match: <version>`; a stale version gets `VERSION_CONFLICT`.
  - The body is `{lock_modes, approved_mode_ids, member_spend_cap_paise}`.
  - Gating from `plan_features`:
    - `lock_modes` needs `org_pro` or above;
    - `member_spend_cap_paise` needs `payg`;
    - otherwise `FEATURE_NOT_IN_PLAN`.
  - Each write is audited and enqueues `org.policy.changed` with the full policy.
  - billing keeps reading the spend cap from `/internal/orgs/{id}`.
- **P10b: Usage reports.** `GET /org/v1/orgs/{id}/usage?from=&to=&format=json|csv`: owner or admin, and `usage_reports` in the plan.
  - `from`/`to` must be at most 92 days apart.
  - It calls billing's `/internal/orgs/{id}/usage` through `clients/billing_client.py` (2 s timeout, one retry; billing down gives `UPSTREAM_UNAVAILABLE`).
  - It adds name and email from `known_users`; deleted users show as "Deleted user".
  - CSV goes through `csv_safe`. JSON has `totals` and `members[]` sorted by messages descending.
- **P10c: Seats from billing.**
  - A `SeatSource` port with two implementations: `StaffSeatSource`, the current one, and `BillingSeatSource`.
  - `BillingSeatSource` consumes `subscription.changed` for `owner_type=org` as a hint, then fetches `GET /internal/orgs/{id}/subscription`. A nightly job, `seats.refresh_all`, catches lost events.
  - It's selected by `SEAT_SOURCE=billing`. When billing's route doesn't exist (404 on the route itself), it logs once and stays on staff.
  - If seats drop below active members, nobody is removed; `/me` shows `over_by`.
- **P10d: Observability.**
  - Metrics: `org_joins_total{source,result}`, `org_seat_full_total`, `org_invites_created_total{kind}`, `org_internal_requests_seconds{route}` (histogram), `org_consumer_lag`, `org_outbox_oldest_age_seconds`, `org_jobs_failed_total{kind}`.
  - Log every 4xx and 5xx with code and correlation id.
  - Add `docs/alerts.md` listing the alerts the owner should set:
    - outbox oldest older than 5 minutes;
    - consumer lag above 1,000;
    - DLQ not empty;
    - internal p99 above 100 ms;
    - job failures.
- **P10e: Load test.** `tests/load/seat_race.py` sends 100 concurrent accepts against 5 free seats through the HTTP API. Exactly 5 succeed, and 95 get `SEATS_FULL`. `tests/load/internal_latency.py` sends 2,000 `GET /internal/memberships/{id}` requests and reports p50, p95 and p99.
- **P10f: Docs and runbook.**
  - `docs/runbook.md` covers how to:
    - create a college pilot;
    - convert a pilot to `org_pro` (in billing first, then seats);
    - fix an org with no owner;
    - re-send failed invites;
    - replay the DLQ;
    - rotate service tokens.
  - Update `README.md` and `docs/lld.md` to match the code.
  - Add a section **"Changes needed in other repos"** with the list below, marked done or not.

**Tests:**
- `tests/api/test_policies.py`: version conflict, plan gating, event payload.
- `tests/api/test_usage_report.py`: fake billing client; CSV safety; billing down gives 503.
- `tests/postgres/test_billing_seat_source.py`.
- The two load scripts are run once by hand, with results in the report.

**Exit check:**
1. The seat race admits exactly 5.
2. Internal p99 is under 20 ms on the owner's machine with a warm database.
3. A college pilot runs end to end on the dev stack: staff creates it, then a CSV of 20 students, then 20 accepts, then billing shows the org plan for all 20, then the usage report lists them.
4. The full list of "changes needed in other repos" is in the report.

---

## Changes needed in other repos (do not edit them; list them in reports)

| Repo | Change | Needed by |
| --- | --- | --- |
| billing-service | Add `GET /internal/orgs/{org_id}/subscription` → `{plan_code, variant, seats, status, period_end}`, and put `seats` and `variant` in `subscription.changed` | Phase 10 (seats from billing) |
| billing-service | Fix `contracts/org-service.md` to the path the client calls (`/internal/memberships/{user_id}`), and to the event fields `role`, `org_type`, `reason` | Docs only |
| billing-service | `scripts/topics.txt` must list the topics this service produces that it does not yet: `organisation.dlq`, `org.policy.changed`, `org.invite.created` and `org.pilot.ending` (`org.member.joined` and `org.member.left` are already there), so `topics.sh` creates them | Phase 4 (the DLQ topic is needed as soon as a message is dead-lettered) |
| billing-service | Add org feature flags to `plans.yaml` (`lock_modes`, `shared_modes_max`, `usage_reports`, `shared_projects`), so `core/plan_features.py` can be removed | Later |
| api-gateway | Route `/org/v1/*` and `/admin/org/v1/*` to `ORGANISATION_SERVICE_URL=http://127.0.0.1:8010`, **forwarding the `Authorization` header** (organisation-service verifies the JWT itself); make `GET /org/v1/invites/preview` a public path (exact match, Phase 7); **never route `/internal/*`** (those are service-to-service, with service tokens) | Phase 6 (the routes exist now) |
| auth-service | Keep `user-events` and `user.deleted` shapes stable; long term, add `email_verified` to the JWT | Phase 9 |
| granthan-frontend | `/join/<token>` page that keeps the token through signup; org admin pages for members, invites, CSV upload with error report, links, domains, policies, usage, audit | Phases 6–10 |
| notification-service (new) | Consume `org.invite.created` and `org.pilot.ending`; then set `INVITE_EMAILS_VIA=notification` here. `org.invite.created` has no link, so organisation-service must first get an **internal route that returns the accept link for an invite** (e.g. `GET /internal/invites/{invite_id}/accept-url`, service token, derives the token for the current `token_version`; **not built yet**) | After Phase 10 |
| conversation-service | Consume `org.policy.changed` and `org.member.left` for mode locking and shared projects | Release plan Phase 3 |
