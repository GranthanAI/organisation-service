# organisation-service: low-level design

Reference for later phases, written from `docs/PHASES.md` (which wins over the HLD where they differ). Later phases update this file.
Status of each part: **P0-P1 built**; everything else is planned (phase in brackets).

## 1. Scope

Owns organisations, memberships and roles, invites (email, CSV, link, domain), seat enforcement, org policies and the admin audit log.
Does not own prices, subscriptions, invoices, wallets (billing-service), login or email verification (auth-service), or conversations (conversation-service, file-service). Org admins never see members' conversations.

## 2. Tables (Postgres `organisation_service_db`, Alembic only) [P2: built, migrations 0001-0005]

Hand-written migrations (`app/db/migrations/versions/`), each with a `downgrade()`; `make db-migrate` goes to head. Times are `timestamptz` (UTC, set from the Clock, no DB defaults); ids are UUIDv7 generated in Python (no DB uuid defaults); emails are `citext` (extension created in 0001); money is `int` paise. Enum columns are `text` with CHECK constraints whose values come from `app/core/enums.py` (no Postgres ENUM types). Models in `app/models/` mirror the columns (`tests/postgres/test_migrations.py` compares them).

### 0001_core
| Table | Columns | Constraints and indexes |
| --- | --- | --- |
| `organisations` | `id` PK, `name`, `slug`, `type`, `legal_name`, `gstin`, `state_code` char(2), `billing_email` citext, `status`, `pilot_ends_at`, `created_by`, `created_at`, `updated_at`, `closed_at` | `uq_organisations_slug`; CHECK type in (college, company); CHECK status in (pilot, active, pilot_expired, suspended, closed); CHECK state_code is two digits; `ix_organisations_status` |
| `memberships` | `id` PK, `org_id` FK, `user_id`, `role`, `status`, `source`, `invited_by`, `joined_at`, `ended_at`, `end_reason` | CHECKs for role, status, source and `end_reason` (left, removed, user_deleted, org_closed); `ck_memberships_ended`: active rows have no `ended_at`/`end_reason`, ended rows have both. **`ux_memberships_one_active_org`** unique `(user_id) WHERE status='active'` (D1); `ix_memberships_org_status (org_id, status)`; `ix_memberships_active_owners (org_id) WHERE status='active' AND role='owner'`; `ix_memberships_active_page (org_id, joined_at, id) WHERE status='active'` |
| `seat_state` | `org_id` PK FK, `plan_code`, `variant`, `seats_bought`, `status` (billing's subscription status; staff-sourced rows use `active`), `period_end`, `source`, `source_ref`, `refreshed_at` | one row per org (created with the org in Phase 5); CHECK plan_code in (pilot, org_pro, org_max, payg); CHECK variant; CHECK source in (staff, billing); `ck_seat_state_status` in (incomplete, active, past_due, suspended, cancelling, ended) (0005); `ck_seat_state_seats_bought`: NULL (no cap) or >= 1. The row locked `FOR UPDATE` on every join |
| `known_users` | `user_id` PK, `email` citext, `full_name`, `email_verified_at`, `deleted_at`, `updated_at` | `ix_known_users_email` |
| `audit_log` | `id` PK, `org_id`, `actor_id`, `actor_kind`, `action`, `target_type`, `target_id` text, `details` jsonb, `reason`, `correlation_id`, `at` | CHECK actor_kind in (user, staff, service, system); no FK on `org_id` (the log outlives anything); `ix_audit_log_org_at (org_id, at DESC, id DESC)`; `ix_audit_log_org_action`. **Insert-only:** trigger `audit_log_insert_only` (BEFORE UPDATE OR DELETE, FOR EACH ROW) runs `forbid_change()`, which raises `restrict_violation` (23001). TRUNCATE is not guarded |
| `outbox` | `id` PK, `topic`, `key`, `payload` jsonb, `created_at`, `attempts` (0), `next_attempt_at`, `published_at`, `last_error` | `ix_outbox_unpublished (next_attempt_at) WHERE published_at IS NULL` |
| `processed_events` | `consumer`, `event_id` text, `processed_at` | PK `(consumer, event_id)`; `ix_processed_events_processed_at` (cleanup) |
| `idempotency_keys` | `key`, `actor_id`, `route`, `request_hash`, `state`, `locked_until`, `status_code` (NULL until done), `response` jsonb (NULL until done), `created_at` | PK `(actor_id, route, key)`; `ck_idempotency_keys_state` state in (in_progress, done); `ix_idempotency_keys_created_at`; kept 24 h (0005) |
| `jobs` | `id` PK, `kind`, `payload` jsonb, `run_at`, `attempts` (0), `locked_until`, `last_error`, `done_at`, `dedupe_key` | `ix_jobs_due (run_at) WHERE done_at IS NULL`; `ux_jobs_open_dedupe (dedupe_key) WHERE dedupe_key IS NOT NULL AND done_at IS NULL` (a recurring job is never queued twice) |

### 0002_invites
| Table | Columns | Constraints and indexes |
| --- | --- | --- |
| `invite_batches` | `id` PK, `org_id` FK, `uploaded_by`, `filename`, `rows_total`, `rows_ok`, `rows_failed`, `status`, `errors` jsonb, `created_at`, `finished_at` | CHECK status in (validating, queued, sending, done, failed); CHECK `errors` is an array of at most 5,000 items; `ix_invite_batches_org` |
| `invites` | `id` PK, `org_id` FK, `kind`, `email` citext, `role`, `token_hash`, `max_uses`, `uses` (0), `batch_id` FK, `domain_restricted` (false), `expires_at`, `accepted_at`, `accepted_by`, `revoked_at`, `revoke_reason`, `created_by`, `created_at` | `uq_invites_token_hash`; CHECK kind in (email, csv, link); CHECK role; links have no email (`kind <> 'link' OR email IS NULL`; the 90-day purge also nulls it); `max_uses` NULL or >= 1; `uses >= 0`; `ck_invites_revoke_reason` in (revoked, expired, resent, user_deleted) and `ck_invites_revoked_together` (`revoked_at` and `revoke_reason` both NULL or both set) (0005). **`ux_invites_open_email`** unique `(org_id, email) WHERE kind <> 'link' AND accepted_at IS NULL AND revoked_at IS NULL`; `ix_invites_org_created`, `ix_invites_batch`, `ix_invites_email` |

### 0003_domains_policies
| Table | Columns | Constraints and indexes |
| --- | --- | --- |
| `org_domains` | `org_id` FK, `domain`, `verify_token`, `verified_at`, `verified_by`, `verify_method`, `auto_join` (false) | PK `(org_id, domain)`; domain is lower-case; CHECK verify_method in (dns, staff); `verified_at` and `verify_method` are both set or both NULL. **`ux_org_domains_verified`** unique `(domain) WHERE verified_at IS NOT NULL` |
| `join_requests` | `id` PK, `org_id` FK, `user_id`, `domain`, `status`, `created_at`, `decided_by`, `decided_at` | CHECK status in (pending, approved, declined, expired); `ix_join_requests_org_status`; **`ux_join_requests_pending`** unique `(org_id, user_id) WHERE status = 'pending'` (0005; maps to `JoinRequestPending`, 409) |
| `org_policies` | `org_id` PK FK, `lock_modes` bool, `approved_mode_ids` uuid[], `member_spend_cap_paise` bigint, `version` (1), `updated_by`, `updated_at` | spend cap NULL (none) or >= 0; `version >= 1` |

### 0004_grants
If a role `org_app` exists: `SELECT, INSERT, UPDATE, DELETE` on every table except `audit_log` (`SELECT, INSERT` only). Skipped silently when the role does not exist (dev uses `postgres`); the trigger is the guard either way.

### 0005_plumbing_fixes
H1 `idempotency_keys`: `status_code` and `response` nullable, new `state` and `locked_until` (a reservation exists before its response). H2 `ux_join_requests_pending`. H3 `ck_seat_state_status`. H4 `invites.revoke_reason` with `ck_invites_revoke_reason` and `ck_invites_revoked_together` (existing revoked rows are backfilled with `revoked`). Downgrade drops these, and deletes idempotency reservations (they cannot exist in the old shape).

**Invite expiry rule (implemented in Phase 7):** an invite past `expires_at` is closed by setting `revoked_at = now` and `revoke_reason = 'expired'`. Phase 7 does this lazily, just before creating a new invite to that email, and in the daily job. That frees `ux_invites_open_email`.

### Data access
`app/db/session.py`: `unit_of_work()` yields one session in one transaction (commit on success, rollback on any exception, so outbox and audit rows roll back with the change). `app/db/errors.py` maps `ux_memberships_one_active_org` to `AlreadyInOrg`, `ux_invites_open_email` to `InviteAlreadyOpen`, `ux_org_domains_verified` to `DomainTaken`, `ux_join_requests_pending` to `JoinRequestPending`; other violations are re-raised unchanged. Repositories (`app/repositories/`) take the session, never commit, and hold no business rules; `AuditRepository` has no update or delete method. `MembershipRepository.list_page` is keyset-paged on `(joined_at, id)`.

## 3. Routes

Public routes verify the Bearer JWT (HS256, `JWT_SECRET_KEY`; claims `sub, email, roles, jti, exp, iat, iss`). Staff is the `roles` claim: `admin` implies `billing_admin` and `support`; `support` is read-only on admin routes. Internal routes take `Authorization: Bearer <token>` matched against `INTERNAL_SERVICE_TOKENS` in constant time.

| Group | Route | Phase |
| --- | --- | --- |
| root | `GET /live`, `GET /ready` (probes Postgres, Redis, Kafka with a timeout; 503 only if Postgres is down, 200 `DEGRADED` if Redis or Kafka is down; body `{status, required, failed, details}`), `GET /metrics` | **P0 built** |
| internal | `GET /internal/ping` -> `{"caller": name}` (checks a service token) | **P3 built** |
| internal | `GET /internal/memberships/{user_id}` and alias `GET /internal/users/{user_id}/membership` -> `{org_id, org_type, role, status}` or 404 | P5 |
| internal | `GET /internal/orgs/{org_id}` -> `{org_id, name, type, gstin, billing_email, state_code, member_spend_cap_paise}` | P5 |
| internal | `GET /internal/orgs/{org_id}/members` -> `{user_ids}` (active only, max 10,000, else 422) | P5 |
| admin `/admin/org/v1` | `POST /orgs`, `GET /orgs?q=&status=&cursor=`, `GET /orgs/{id}`, `POST /orgs/{id}/members`, `POST /orgs/{id}/seats`, `POST /orgs/{id}/suspend`, `/reactivate`, `/close`, `/extend-pilot`; every write needs `reason` (>= 5 chars) and staff `admin` | P5 |
| admin | `POST /orgs/{id}/domains/{domain}/manual-verify` | P9 |
| v1 `/org/v1` | `GET /me`; `GET/PATCH /orgs/{id}` | P6 |
| v1 | `GET /orgs/{id}/members`, `PATCH/DELETE /orgs/{id}/members/{user_id}`, `POST /orgs/{id}/leave`, `POST /orgs/{id}/transfer-ownership`, `GET /orgs/{id}/audit` | P6 |
| v1 | `POST /orgs/{id}/invites`, `GET /orgs/{id}/invites`, `POST .../invites/{id}/resend`, `DELETE .../invites/{id}`, `GET /invites/preview?token=` (no JWT), `POST /invites/accept` | P7 |
| v1 | `POST /orgs/{id}/invites/csv`, `GET /invite-batches/{id}`, `GET /invite-batches/{id}/errors.csv`, `POST/GET/DELETE /orgs/{id}/invite-links` | P8 |
| v1 | `POST/GET /orgs/{id}/domains`, `POST .../domains/{domain}/verify`, `PATCH/DELETE .../domains/{domain}`, `GET .../join-requests`, `POST .../join-requests/{id}/approve\|decline` | P9 |
| v1 | `GET/PUT /orgs/{id}/policies` (If-Match), `GET /orgs/{id}/usage?from=&to=&format=` | P10 |

Calls out to billing-service (`clients/billing_client.py`, same bearer token, 2 s timeout, one retry): `GET /internal/orgs/{org_id}/usage?from=&to=` (exists); `GET /internal/orgs/{org_id}/subscription` (not built in billing yet; seats come from staff until Phase 10).

## 4. Events

Every event carries `event_id`, `schema_version: "1.0"`, `occurred_at` (ISO UTC, `Z`). Published through the outbox [P4].

| Topic | Payload | Key |
| --- | --- | --- |
| `org.member.joined` | `{org_id, user_id, role, org_type, source}` | `user_id` |
| `org.member.left` | `{org_id, user_id, role, org_type, reason: left\|removed\|user_deleted\|org_closed}` | `user_id` |
| `org.policy.changed` | `{org_id, version, lock_modes, approved_mode_ids, member_spend_cap_paise}` | `org_id` |
| `org.invite.created` | `{org_id, invite_id, email, org_name, inviter_name, accept_url, expires_at}` | `org_id` |
| `org.pilot.ending` | `{org_id, ends_at, days_left}` | `org_id` |
| `organisation.dlq` | dead letters of consumed topics | n/a |

Always send `org_type` on member events (billing defaults to `company` otherwise). Member events are keyed by `user_id`.

Consumed: `user-events` (`UserRegistered`, `EmailVerified` inside `{event_type, payload, created_at}`; other types ignored), `user.deleted` (`{event_type, schema_version, event_id, occurred_at, user_id}`), `subscription.changed` (Phase 10 hint). Deduplicated on `processed_events`; 3 retries, then `organisation.dlq`.

## 5. Errors and request plumbing [P3: built]

Every error is `application/problem+json`: `{type, title, status, code, detail, correlation_id}` (`app/middleware/exception_handler.py`). The code-to-status map is ONE dict, `app/core/error_codes.py:ERROR_STATUS`; `tests/api/test_problem_json.py` checks that every `DomainError` subclass has an entry and that the statuses match this table.

| Code | HTTP | Raised as |
| --- | --- | --- |
| `UNAUTHENTICATED` | 401 (`WWW-Authenticate: Bearer`) | `Unauthenticated`: no, bad, expired, revoked or deleted-user credentials; the detail never says which |
| `INVITE_EMAIL_MISMATCH`, `EMAIL_NOT_VERIFIED`, `FEATURE_NOT_IN_PLAN`, `FORBIDDEN_ROLE` | 403 | `InviteEmailMismatch`, `EmailNotVerified`, `FeatureNotInPlan`, `ForbiddenRole` |
| `NOT_FOUND`, `INVITE_INVALID` | 404 | `NotFound` (also: not a member of the org in the path), `InviteInvalid` |
| `INVITE_EXPIRED` | 410 | `InviteExpired` |
| `SEATS_FULL`, `PILOT_FULL`, `ALREADY_IN_ORG`, `LAST_OWNER`, `ORG_INACTIVE`, `INVITE_ALREADY_OPEN`, `DOMAIN_TAKEN`, `JOIN_REQUEST_PENDING` | 409 | the same names in CamelCase |
| `IDEMPOTENCY_IN_PROGRESS` | 409 (`Retry-After`) | `IdempotencyInProgress` |
| `VERSION_CONFLICT` | 412 | `VersionConflict` |
| `PUBLIC_EMAIL_DOMAIN`, `CSV_INVALID`, `VALIDATION_ERROR`, `IDEMPOTENCY_MISMATCH` | 422 | `PublicEmailDomain`, `CsvInvalid`, `ValidationFailed` (also illegal transitions and role changes), `IdempotencyMismatch` |
| `RATE_LIMITED` | 429 (`Retry-After`) | `RateLimited` |
| `UPSTREAM_UNAVAILABLE` | 503 | `UpstreamUnavailable` |
| `INTERNAL_ERROR` | 500 | any other exception: no internals in the body, stack trace and correlation id in the log |

`RequestValidationError` gives 422 `VALIDATION_ERROR` with `errors: [{field, message}]` and never the input values. Starlette HTTP errors (unknown route, wrong method) give problem+json with `NOT_FOUND` or `METHOD_NOT_ALLOWED`.

### Identity
- **Users** (`app/security/`): `verify_jwt_token` checks HS256 with `JWT_SECRET_KEY`, requires `sub, email, jti, exp, iat, iss`, ALWAYS verifies the issuer (`JWT_ISSUER`, default `https://auth.granthan.com`) and allows 10 s leeway. `get_current_user` then checks Redis `blacklist:<jti>` (auth-service writes it on logout): Redis down lets GET/HEAD/OPTIONS through (warning logged) and refuses POST/PUT/PATCH/DELETE with 503 `UPSTREAM_UNAVAILABLE`. A `known_users.deleted_at` gives 401. `CurrentUser {id, email, roles, jti, staff_role}`; `staff_role` is `admin` if `"admin" in roles`, else `support`, else none (`billing_admin` alone is not staff here).
- `require_staff(min_role)`: SUPPORT allows support and admin; ADMIN allows admin only; others get 403 `FORBIDDEN_ROLE`.
- `require_org_role(action)`: reads `org_id` from the path, loads the caller's ACTIVE membership, runs `permission_rules.check`; not a member (never joined, left, removed) is 404 `NOT_FOUND`, never 403. Returns `OrgContext {org_id, user, role, membership_id}`. An `OwnRoleOnly` cell passes the dependency; the use case checks the target's role. Staff do not use it.
- **Services:** `INTERNAL_SERVICE_TOKENS` (`name:token,...`) is parsed once at startup (empty names, empty tokens, duplicate names and shared tokens refuse to boot); `require_service()` compares the bearer with every token using `hmac.compare_digest` without returning early; a miss is 401 and writes nothing. billing's `ORG_SERVICE_TOKEN` must equal the `billing:` entry.
- Nothing logs the Authorization header, a raw token or a jti.

### Idempotency (`IdempotentRoute`, `services/idempotency_service.py`)
Public and admin routers use `APIRouter(route_class=IdempotentRoute)`. Every POST needs `Idempotency-Key` (8-128 of `[A-Za-z0-9_-]`, else 422). Scope `(actor_id, route template, key)`; `request_hash` = sha256 of the canonical JSON body.
1. **Reserve** `state='in_progress'`, `locked_until = now + 60 s`, in its own short transaction.
2. **On conflict**, read the row: hash differs gives 422 `IDEMPOTENCY_MISMATCH`; `done` replays the stored status and body with `Idempotent-Replayed: true`; `in_progress` and locked gives 409 `IDEMPOTENCY_IN_PROGRESS` with `Retry-After`; lock passed (or the row is older than 24 h) is taken over atomically.
3. **After the handler:** 2xx and 4xx are stored (`state='done'`); 5xx, an exception, 401 and 429 delete the reservation so the client can retry.
TODO (Phase 4): a job that deletes `idempotency_keys` older than 24 h (until then old rows are ignored on read and taken over).

### Rate limits (`middleware/rate_limit.py`, buckets in `app/core/constants.py`)
Redis fixed windows (`rl:<bucket>:<who>:<window>`), `INCR` and `EXPIRE` in one pipeline; over the limit is 429 `RATE_LIMITED` with `Retry-After` (seconds to the window end). Redis down: `fail_closed` buckets answer 503, the others pass. Per-IP buckets use `X-Forwarded-For` only when the direct peer is in `FORWARDED_ALLOW_IPS` (right-most address that is not itself a trusted proxy).

| Bucket | Limit | Key | Redis down |
| --- | --- | --- | --- |
| `invite_create` | 30 per minute | user | fail closed (503) |
| `csv_upload` | 5 per hour | user | fail closed (503) |
| `invite_preview` | 30 per minute | IP | pass |
| `invite_accept` | 10 per minute | user | pass |

### Audit (`services/audit_service.py`)
`record(session, actor, action, target_type, target_id, details=None, reason=None, *, org_id=None, staff=False)` writes one `audit_log` row in the CALLER's session (never its own transaction, never commits). `actor_kind`: `user` (CurrentUser), `staff` (CurrentUser with `staff=True`, acting through an admin route), `service` (ServiceCaller; the name goes in `details.service`), `system`. `correlation_id` comes from the request ContextVar. `details` must be JSON-serialisable, at most 8 KB, and no key (at any depth) may contain `token`, `secret` or `password` (ValueError). Action names are constants in `app/core/constants.py`.

## 6. Rules (Phase 1, pure Python in `app/services/*_rules.py`)

- **Enums** (`app/core/enums.py`): `OrgType`, `OrgStatus`, `Role`, `MembershipStatus`, `JoinSource`, `InviteKind`, `StaffRole`.
- **Permissions**: one `(Action, Role | StaffRole) -> Allowed | Denied | OwnRoleOnly` table; `check(action, actor)` raises `ForbiddenRole`. Admins remove members only, not other admins or the owner. `OwnRoleOnly` = allowed only against a target whose role ranks below the actor's. The owner may leave (the last-owner rule, not this table, blocks it). `CLOSE_ORG` is the org owner closing their own org; `STAFF_CLOSE_ORG` is staff closing any org. The matrix (`services/permission_rules.py`, walked cell by cell in `tests/unit/test_permission_rules.py`):

| Action | Owner | Admin | Member | Staff support | Staff admin |
| --- | --- | --- | --- | --- | --- |
| `view_org` | Yes | Yes | Yes | Yes | Yes |
| `view_members` | Yes | Yes | - | Yes | Yes |
| `view_invites` | Yes | Yes | - | Yes | Yes |
| `manage_join_requests` | Yes | Yes | - | - | Yes |
| `invite` | Yes | Yes | - | - | Yes |
| `revoke_invite` | Yes | Yes | - | - | Yes |
| `csv_upload` | Yes | Yes | - | - | Yes |
| `manage_links` | Yes | Yes | - | - | Yes |
| `remove_member` | Yes | Lower roles only | - | - | Yes |
| `change_role` | Yes | - | - | - | Yes |
| `transfer_ownership` | Yes | - | - | - | Yes |
| `close_org` | Yes | - | - | - | Yes |
| `edit_details` | Yes | Yes | - | - | Yes |
| `manage_domains` | Yes | Yes | - | - | Yes |
| `edit_policies` | Yes | Yes | - | - | Yes |
| `view_usage` | Yes | Yes | - | Yes | Yes |
| `view_audit` | Yes | Yes | - | Yes | Yes |
| `leave` | Yes | Yes | Yes | - | - |
| `staff_create_org` | - | - | - | - | Yes |
| `staff_suspend` | - | - | - | - | Yes |
| `staff_extend_pilot` | - | - | - | - | Yes |
| `staff_manual_domain_verify` | - | - | - | - | Yes |
| `staff_set_seats` | - | - | - | - | Yes |
| `staff_add_member` | - | - | - | - | Yes |
| `staff_reactivate` | - | - | - | - | Yes |
| `staff_close_org` | - | - | - | - | Yes |

- **Seats**: `can_join(org_status, seats_bought | None, active_count, is_pilot)`; `None` = no cap (PAYG); pilots capped at `PILOT_MAX_MEMBERS`. Owners and admins count (D3). No overage (D2).
- **Lifecycle**: `pilot -> active|pilot_expired|suspended|closed`; `pilot_expired -> pilot|active|closed`; `active -> suspended|closed`; `suspended -> pilot|pilot_expired|active|closed`; `closed` final. `reactivation_target(was_pilot, pilot_ends_at, now)` picks `pilot` (pilot still running), `pilot_expired` (it ended) or `active`.
- **Membership**: `check_can_end(role, active_owner_count, reason: EndReason)`: last-owner protection (`LastOwner`) applies to `left` and `removed` only; `user_deleted` and `org_closed` always pass and return `owner_missing=True` when the last owner goes (Phase 4 writes the `org.owner_missing` audit row); transfer target must be an active admin and the old owner becomes admin.
- **Layering** (`tests/unit/test_layering.py`): `*_rules.py` import only stdlib and `app/core`; `api/` never imports `models/` or `repositories/`; `repositories/` never import `api/`.

## 7. Plans and seats (`app/core/plan_features.py` is the only home of this table) [P6]

| Plan | Seats | Lock modes | Shared modes | Usage reports | Spend cap |
| --- | --- | --- | --- | --- | --- |
| `pilot` | up to 25, 30 days | no | 3 | no | no |
| `org_pro` | min 50 (college) / 5 (company) | yes | 20 | yes | no |
| `org_max` | min 10 (college) / 5 (company) | yes | 50 | yes | no |
| `payg` | no cap | yes | unlimited | yes | yes |

## 8. Settings

See `.env.example` for every setting with its comment: service (`ENVIRONMENT`, `SERVICE_PORT=8010`, `DATABASE_URL`, `DB_*`, `REDIS_URL`, `KAFKA_*`), identity (`JWT_*`, `INTERNAL_SERVICE_TOKENS`), billing (`BILLING_BASE_URL`, `BILLING_SERVICE_TOKEN`), `WORKERS_ENABLED`, email (`EMAIL_SENDER`, `SMTP_*`, `APP_BASE_URL`), invites (`INVITE_TTL_DAYS`, `LINK_MAX_TTL_DAYS`, `INVITES_PER_ORG_PER_DAY`, `CSV_MAX_*`), pilots and seats (`PILOT_MAX_MEMBERS`, `PILOT_DAYS`, `SEAT_SOURCE`), `DOMAIN_AUTO_JOIN_ENABLED`. Production refuses dev defaults (`Settings._production_problems`).

## 9. Defaults D1-D6

D1 one active org per user (partial unique index). D2 joins hard-stop when full. D3 owners and admins count as seats. D4 only staff create organisations in v1. D5 domain auto-join off unless `DOMAIN_AUTO_JOIN_ENABLED=true`. D6 org admins see names, emails and usage counts, never content.
