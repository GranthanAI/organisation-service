# organisation-service: low-level design

Reference for later phases, written from `docs/PHASES.md` (which wins over the HLD where they differ). Later phases update this file.
Status of each part: **P0-P1 built**; everything else is planned (phase in brackets).

## 1. Scope

Owns organisations, memberships and roles, invites (email, CSV, link, domain), seat enforcement, org policies and the admin audit log.
Does not own prices, subscriptions, invoices, wallets (billing-service), login or email verification (auth-service), or conversations (conversation-service, file-service). Org admins never see members' conversations.

## 2. Tables (Postgres `organisation_service_db`, Alembic only) [P2]

Times are `timestamptz` (UTC); emails are `citext`, stored lower-cased and trimmed; ids are UUIDv7; money is `int` paise.

### 0001_core
| Table | Columns and rules |
| --- | --- |
| `organisations` | `id`, `name`, `slug` (unique), `type`, `legal_name`, `gstin`, `state_code` (char 2), `billing_email`, `status`, `pilot_ends_at`, `created_by`, `created_at`, `updated_at`, `closed_at` |
| `memberships` | `id`, `org_id` FK, `user_id`, `role`, `status`, `source`, `invited_by`, `joined_at`, `ended_at`, `end_reason`. Partial unique index `ux_memberships_one_active_org` on `(user_id) WHERE status='active'` (D1). Index `(org_id, status)` |
| `seat_state` | `org_id` PK FK, `plan_code` (`pilot`\|`org_pro`\|`org_max`\|`payg`), `variant` (`college`\|`company`), `seats_bought` (null = no cap), `status`, `period_end`, `source` (`staff`\|`billing`), `source_ref`, `refreshed_at`. Row locked `FOR UPDATE` on every join |
| `known_users` | `user_id` PK, `email`, `full_name`, `email_verified_at`, `deleted_at`, `updated_at`. Index on `email` |
| `audit_log` | `id`, `org_id`, `actor_id`, `actor_kind` (`user`\|`staff`\|`service`\|`system`), `action`, `target_type`, `target_id`, `details` jsonb, `reason`, `correlation_id`, `at`. Insert-only: trigger `forbid_change()` raises on UPDATE/DELETE |
| `outbox` | `id`, `topic`, `key`, `payload` jsonb, `created_at`, `attempts`, `next_attempt_at`, `published_at`, `last_error`. Index on unpublished rows |
| `processed_events` | `consumer`, `event_id`, `processed_at`; PK `(consumer, event_id)` |
| `idempotency_keys` | `key`, `actor_id`, `route`, `request_hash`, `status_code`, `response` jsonb, `created_at`; PK `(actor_id, route, key)`; kept 24 h |
| `jobs` | `id`, `kind`, `payload`, `run_at`, `attempts`, `locked_until`, `last_error`, `done_at` |

### 0002_invites
| Table | Columns and rules |
| --- | --- |
| `invites` | `id`, `org_id`, `kind`, `email` (null for links), `role`, `token_hash` (unique), `max_uses`, `uses`, `batch_id`, `domain_restricted`, `expires_at`, `accepted_at`, `accepted_by`, `revoked_at`, `created_by`, `created_at`. Partial unique `(org_id, email) WHERE kind <> 'link' AND accepted_at IS NULL AND revoked_at IS NULL` |
| `invite_batches` | `id`, `org_id`, `uploaded_by`, `filename`, `rows_total`, `rows_ok`, `rows_failed`, `status` (`validating`\|`queued`\|`sending`\|`done`\|`failed`), `errors` jsonb (max 5,000 rows), `created_at`, `finished_at` |

### 0003_domains_policies
| Table | Columns and rules |
| --- | --- |
| `org_domains` | `org_id`, `domain`, `verify_token`, `verified_at`, `verified_by`, `verify_method` (`dns`\|`staff`), `auto_join`. Partial unique `(domain) WHERE verified_at IS NOT NULL` |
| `join_requests` | `id`, `org_id`, `user_id`, `domain`, `status` (`pending`\|`approved`\|`declined`\|`expired`), `created_at`, `decided_by`, `decided_at` |
| `org_policies` | `org_id` PK, `lock_modes`, `approved_mode_ids` uuid[], `member_spend_cap_paise` (null = none), `version`, `updated_by`, `updated_at` |

## 3. Routes

Public routes verify the Bearer JWT (HS256, `JWT_SECRET_KEY`; claims `sub, email, roles, jti, exp, iat, iss`). Staff is the `roles` claim: `admin` implies `billing_admin` and `support`; `support` is read-only on admin routes. Internal routes take `Authorization: Bearer <token>` matched against `INTERNAL_SERVICE_TOKENS` in constant time.

| Group | Route | Phase |
| --- | --- | --- |
| root | `GET /live`, `GET /ready` (probes Postgres, Redis, Kafka with a timeout; 503 only if Postgres is down, 200 `DEGRADED` if Redis or Kafka is down; body `{status, required, failed, details}`), `GET /metrics` | **P0 built** |
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

## 5. Errors (`application/problem+json`: `{type, title, status, code, detail, correlation_id}`) [P3]

| Code | HTTP |
| --- | --- |
| `SEATS_FULL`, `PILOT_FULL`, `ALREADY_IN_ORG`, `LAST_OWNER`, `ORG_INACTIVE`, `INVITE_ALREADY_OPEN`, `DOMAIN_TAKEN` | 409 |
| `INVITE_INVALID` | 404 |
| `INVITE_EXPIRED` | 410 |
| `INVITE_EMAIL_MISMATCH`, `EMAIL_NOT_VERIFIED`, `FEATURE_NOT_IN_PLAN`, `FORBIDDEN_ROLE` | 403 |
| `VERSION_CONFLICT` | 412 |
| `PUBLIC_EMAIL_DOMAIN`, `CSV_INVALID`, `VALIDATION_ERROR`, `IDEMPOTENCY_MISMATCH` | 422 |
| `RATE_LIMITED` | 429 |
| `UPSTREAM_UNAVAILABLE` | 503 |

## 6. Rules (Phase 1, pure Python in `app/services/*_rules.py`)

- **Enums** (`app/core/enums.py`): `OrgType`, `OrgStatus`, `Role`, `MembershipStatus`, `JoinSource`, `InviteKind`, `StaffRole`.
- **Permissions**: one `(Action, Role | StaffRole) -> Allowed | Denied | OwnRoleOnly` table; `check(action, actor)` raises `ForbiddenRole`. Admins remove members only, not other admins or the owner.
- **Seats**: `can_join(org_status, seats_bought | None, active_count, is_pilot)`; `None` = no cap (PAYG); pilots capped at `PILOT_MAX_MEMBERS`. Owners and admins count (D3). No overage (D2).
- **Lifecycle**: `pilot -> active|pilot_expired|suspended|closed`; `pilot_expired -> pilot|active|closed`; `active -> suspended|closed`; `suspended -> pilot|pilot_expired|active|closed`; `closed` final. `reactivation_target(was_pilot, pilot_ends_at, now)` picks `pilot` (pilot still running), `pilot_expired` (it ended) or `active`.
- **Membership**: last-owner protection; transfer target must be an active admin and the old owner becomes admin.
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
