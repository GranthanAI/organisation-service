# organisation-service: low-level design

Reference for later phases, written from `docs/PHASES.md` (which wins over the HLD where they differ). Later phases update this file.
Status of each part: **P0-P1 built**; everything else is planned (phase in brackets).

## 1. Scope

Owns organisations, memberships and roles, invites (email, CSV, link, domain), seat enforcement, org policies and the admin audit log.
Does not own prices, subscriptions, invoices, wallets (billing-service), login or email verification (auth-service), or conversations (conversation-service, file-service). Org admins never see members' conversations.

## 2. Tables (Postgres `organisation_service_db`, Alembic only) [P2: built, migrations 0001-0006]

Hand-written migrations (`app/db/migrations/versions/`), each with a `downgrade()`; `make db-migrate` goes to head. Times are `timestamptz` (UTC, set from the Clock, no DB defaults); ids are UUIDv7 generated in Python (no DB uuid defaults); emails are `citext` (extension created in 0001); money is `int` paise. Enum columns are `text` with CHECK constraints whose values come from `app/core/enums.py` (no Postgres ENUM types). Models in `app/models/` mirror the columns (`tests/postgres/test_migrations.py` compares them).

### 0001_core
| Table | Columns | Constraints and indexes |
| --- | --- | --- |
| `organisations` | `id` PK, `name`, `slug`, `type`, `legal_name`, `gstin`, `state_code` char(2), `billing_email` citext, `status`, `pilot_ends_at`, `created_by`, `created_at`, `updated_at`, `closed_at` | `uq_organisations_slug`; CHECK type in (college, company); CHECK status in (pilot, active, pilot_expired, suspended, closed); CHECK state_code is two digits; `ix_organisations_status` |
| `memberships` | `id` PK, `org_id` FK, `user_id`, `role`, `status`, `source`, `invited_by`, `joined_at`, `ended_at`, `end_reason` | CHECKs for role, status, source and `end_reason` (left, removed, user_deleted, org_closed); `ck_memberships_ended`: active rows have no `ended_at`/`end_reason`, ended rows have both. **`ux_memberships_one_active_org`** unique `(user_id) WHERE status='active'` (D1); `ix_memberships_org_status (org_id, status)`; `ix_memberships_active_owners (org_id) WHERE status='active' AND role='owner'`; `ix_memberships_active_page (org_id, joined_at, id) WHERE status='active'` |
| `seat_state` | `org_id` PK FK, `plan_code`, `variant`, `seats_bought`, `status` (billing's subscription status; staff-sourced rows use `active`), `period_end`, `source`, `source_ref`, `refreshed_at` | one row per org (created with the org in Phase 5); CHECK plan_code in (pilot, org_pro, org_max, payg); CHECK variant; CHECK source in (staff, billing); `ck_seat_state_status` in (incomplete, active, past_due, suspended, cancelling, ended) (0005); `ck_seat_state_seats_bought`: NULL (no cap) or >= 1. The row locked `FOR UPDATE` on every join |
| `known_users` | `user_id` PK, `email` citext, `full_name`, `email_verified_at`, `deleted_at`, `updated_at` | `ix_known_users_email` |
| `audit_log` | `id` PK, `org_id`, `actor_id`, `actor_kind`, `action`, `target_type`, `target_id` text, `details` jsonb, `reason`, `correlation_id`, `at` | CHECK actor_kind in (user, staff, service, system); no FK on `org_id` (the log outlives anything); `ix_audit_log_org_at (org_id, at DESC, id DESC)`; `ix_audit_log_org_action`. **Insert-only:** trigger `audit_log_insert_only` (BEFORE UPDATE OR DELETE, FOR EACH ROW) runs `forbid_change()`, which raises `restrict_violation` (23001). TRUNCATE is not guarded |
| `outbox` | `id` PK, `topic`, `key`, `payload` jsonb, `created_at`, `attempts` (0), `next_attempt_at`, `published_at`, `last_error`, `correlation_id` (0006) | `ix_outbox_unpublished (next_attempt_at) WHERE published_at IS NULL` |
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

### 0006_outbox_correlation
`outbox.correlation_id` (nullable): the relay runs long after the request, so the id is stored with the row and goes out as the `correlation_id` Kafka header.

### Data access
`app/db/session.py`: `unit_of_work()` yields one session in one transaction (commit on success, rollback on any exception, so outbox and audit rows roll back with the change). `app/db/errors.py` maps `ux_memberships_one_active_org` to `AlreadyInOrg`, `ux_invites_open_email` to `InviteAlreadyOpen`, `ux_org_domains_verified` to `DomainTaken`, `ux_join_requests_pending` to `JoinRequestPending`; other violations are re-raised unchanged. Repositories (`app/repositories/`) take the session, never commit, and hold no business rules; `AuditRepository` has no update or delete method. `MembershipRepository.list_page` is keyset-paged on `(joined_at, id)`.

## 3. Routes

Public routes verify the Bearer JWT (HS256, `JWT_SECRET_KEY`; claims `sub, email, roles, jti, exp, iat, iss`). Staff is the `roles` claim: `admin` implies `billing_admin` and `support`; `support` is read-only on admin routes. Internal routes take `Authorization: Bearer <token>` matched against `INTERNAL_SERVICE_TOKENS` in constant time.

| Group | Route | Phase |
| --- | --- | --- |
| root | `GET /live`, `GET /ready` (probes Postgres, Redis, Kafka with a timeout; 503 only if Postgres is down, 200 `DEGRADED` if Redis or Kafka is down; body `{status, required, failed, details}`), `GET /metrics` | **P0 built** |
| internal | `GET /internal/ping` -> `{"caller": name}` (checks a service token) | **P3 built** |
| internal | `GET /internal/memberships/{user_id}` and alias `GET /internal/users/{user_id}/membership` -> `{org_id, org_type, role, status}` or 404 | **P5 built** |
| internal | `GET /internal/orgs/{org_id}` -> `{org_id, name, type, gstin, billing_email, state_code, member_spend_cap_paise}` or 404 | **P5 built** |
| internal | `GET /internal/orgs/{org_id}/members` -> `{user_ids}` (active only, oldest first, max 10,000, else 422) | **P5 built** |
| admin `/admin/org/v1` | `POST /orgs`, `GET /orgs?q=&status=&cursor=`, `GET /orgs/{id}`, `POST /orgs/{id}/members`, `POST /orgs/{id}/seats`, `POST /orgs/{id}/suspend`, `/reactivate`, `/close`, `/extend-pilot`; every write needs `reason` (5-500 chars), an `Idempotency-Key` and staff `admin` (reads: staff `support` too) | **P5 built** |
| admin | `POST /orgs/{id}/domains/{domain}/manual-verify` | P9 |
| v1 `/org/v1` | `GET /me`; `GET/PATCH /orgs/{id}` | **P6 built** |
| v1 | `GET /orgs/{id}/members`, `PATCH/DELETE /orgs/{id}/members/{user_id}`, `POST /orgs/{id}/leave`, `POST /orgs/{id}/transfer-ownership`, `GET /orgs/{id}/audit` | **P6 built** |
| admin | `GET /admin/org/v1/orgs/{id}/members`, `GET /admin/org/v1/orgs/{id}/audit` (staff `support` may read) | **P6 built** |
| v1 | `POST /orgs/{id}/invites`, `GET /orgs/{id}/invites`, `POST .../invites/{id}/resend`, `DELETE .../invites/{id}`, `GET /invites/preview?token=` (no JWT), `POST /invites/accept` | P7 |
| v1 | `POST /orgs/{id}/invites/csv`, `GET /invite-batches/{id}`, `GET /invite-batches/{id}/errors.csv`, `POST/GET/DELETE /orgs/{id}/invite-links` | P8 |
| v1 | `POST/GET /orgs/{id}/domains`, `POST .../domains/{domain}/verify`, `PATCH/DELETE .../domains/{domain}`, `GET .../join-requests`, `POST .../join-requests/{id}/approve\|decline` | P9 |
| v1 | `GET/PUT /orgs/{id}/policies` (If-Match), `GET /orgs/{id}/usage?from=&to=&format=` | P10 |

Calls out to billing-service (`clients/billing_client.py`, same bearer token, 2 s timeout, one retry): `GET /internal/orgs/{org_id}/usage?from=&to=` (exists); `GET /internal/orgs/{org_id}/subscription` (not built in billing yet; seats come from staff until Phase 10).

## 3b. Phase 5: seats, the join transaction, the billing contract, staff-run organisations [built]

### Join transaction (`services/join_service.py`) - the ONLY code that creates an active membership
`join(session, org_id, user_id, source, role=MEMBER, invite=None, *, actor, staff=False, clock)`, in the caller's transaction:
1. lock the `seat_state` row (`FOR UPDATE`; an org without one is locked through `organisations`): every join and every `membership_service.end` of an organisation queue on this row, so two joins can never take the last seat (`tests/postgres/test_seat_race.py`: 20 concurrent joins into 5 free seats admit exactly 5);
2. refuse a user who already has an active membership (`ALREADY_IN_ORG`, checked first so a repeat join of the same org says so rather than "full"), then `seat_rules.can_join(org status, seats, active count, is_pilot = org is pilot, pilot_max_members = PILOT_MAX_MEMBERS)` and `join_decision_error` (`ORG_INACTIVE`, `SEATS_FULL`, `PILOT_FULL`);
3. refuse a deleted user (`NotFound`);
4. insert the membership (uuid7, `active`, `source`, `invited_by` = the invite's creator, `joined_at`); the unique index `ux_memberships_one_active_org` is still the final guard, and an `IntegrityError` is mapped to `AlreadyInOrg` both by `join` (caller's own flush) and by `unit_of_work`;
5. if an invite was used: `uses += 1`, and for kind email or csv `accepted_at` and `accepted_by` (a link is never "accepted", it only counts uses);
6. audit `member.joined` (`user_id, source, role, invite_id`);
7. enqueue `org.member.joined` (with `org_type`, keyed by `user_id`).
Seats: `seat_state.status` `incomplete` or `ended` (or a missing row) means NO seats are available, because `seats_bought` NULL would otherwise read as "no cap" (payg). The FIRST owner of an organisation is the one exception (seats cannot be bought without an owner). Role `owner` is allowed only when the org has no active owner, or the actor is staff.

### Seats from staff (`services/seat_service.py`, plan table in `app/core/plan_features.py`)
`set_from_staff(session, org_id, plan_code, variant, seats_bought, period_end, actor, reason)`: `plan_code` in pilot, org_pro, org_max, payg; `variant` must equal the org's type; minimums (from billing's `plans.yaml`): org_pro 50 college / 5 company, org_max 10 college / 5 company, below it is 422 naming the minimum; `payg` forces `seats_bought = NULL` (no cap); `pilot` forces `PILOT_MAX_MEMBERS`; sets `status = active`, `source = staff`. A paid plan on a `pilot` or `pilot_expired` org moves it to `active` (through `org_lifecycle_rules`). Seats below the current active count are allowed: nobody is removed, joins stop, and `over_by` is in the `seats.set` audit details together with the old and new values. A closed org is refused (`ORG_INACTIVE`). With `SEAT_SOURCE=billing`: 409 `SEATS_MANAGED_BY_BILLING` ("seats come from billing"); that is Phase 10.

### Internal routes billing calls (`api/internal/*`, `services/directory_service.py`; bearer service token)
- `GET /internal/memberships/{user_id}` (alias `/internal/users/{user_id}/membership`): 404 when the user has no ACTIVE membership; otherwise `{org_id, org_type, role, status}`. **`status` (K4):** `"active"` while the org status is `pilot` or `active`; otherwise the org's own status (`suspended`, `pilot_expired`, `closed`). billing grants the organisation plan only when `status == "active"` (`entitlements.py`), so a suspended or expired organisation stops granting its plan while its members stay members. Never a 4xx other than 404 (billing raises on those).
- `GET /internal/orgs/{org_id}`: `{org_id, name, type, gstin, billing_email, state_code, member_spend_cap_paise}` (cap from `org_policies`, else null); closed orgs are still returned.
- `GET /internal/orgs/{org_id}/members`: `{user_ids}`, active only, oldest first, at most 10,000 (above that 422 and an error log); 404 for an unknown org.
- Each is one cheap query; `org_internal_requests_seconds{route}` (histogram) times them. `tests/support/billing_org_client.py` is billing's own parser copied (commit 11a5fed) and run against these routes.

### Staff routes (`/admin/org/v1`, `api/admin/orgs.py`, `services/org_service.py`)
Writes need staff `admin`, an `Idempotency-Key` (route class `IdempotentRoute`) and a `reason` (5-500 characters, audited; audit `actor_kind` is `staff`); reads allow `support`. Every action also calls `permission_rules.check` with its `STAFF_*` action.
- `POST /orgs {name, type, legal_name?, gstin?, state_code?, billing_email, owner_email, pilot, reason}` (201): one transaction creates the org (slug from the name: lower-case a-z0-9 and `-`, 3-60 characters, `-2`, `-3` on a clash; status `pilot` with `pilot_ends_at = now + PILOT_DAYS`, or `active`), its `seat_state` (pilot: plan `pilot`, 25 seats, `period_end` = the pilot end; otherwise plan `org_pro`, seats NULL, status `incomplete` = no seats until staff set them), a default `org_policies` row (`lock_modes` false, version 1), the audit row, and the owner: a known, not-deleted user with that email joins as OWNER (source staff); otherwise an open owner invite is created (kind email, role owner, expires in `INVITE_TTL_DAYS`, **token hash only**: the raw token is never returned or logged, and nothing is sent before Phase 7) and the response says `owner_invite_pending: true`. For a pilot: `pilot.remind` at ends-7d and ends-1d and `pilot.expire` at the end (dedupe keys per org; a reminder time already past is not scheduled). GSTIN format (15 characters) and the GST state-code list (`core/constants.py`) are checked.
- `GET /orgs?q=&status=&cursor=&limit=` (keyset on `(created_at, id)`, newest first; `q` matches name or slug), `GET /orgs/{id}` (adds `seat_state` and `active_count`).
- `POST /orgs/{id}/members {user_id, role, reason}`: `join(source=staff)`. `POST /orgs/{id}/seats`: `seat_service.set_from_staff`.
- `POST /orgs/{id}/suspend`: `pilot` or `active` becomes `suspended` (the audit records `was_pilot`). `POST /orgs/{id}/reactivate`: `reactivation_target(was_pilot, pilot_ends_at, now)` where `was_pilot` is read from the seat plan (`pilot`): back to `pilot`, to `pilot_expired` if the pilot ended meanwhile, else `active`.
- `POST /orgs/{id}/extend-pilot {days 1-30}`: new end = `max(now, pilot_ends_at) + days`; `pilot_expired` returns to `pilot`; the seat `period_end` follows; the open pilot jobs are replaced (same dedupe keys).
- `POST /orgs/{id}/close`: status `closed` at once (blocks joins), open invites revoked (`revoke_reason = revoked`), the open pilot jobs dropped, and the job `org.close_members` queued.

### Jobs added in Phase 5 (`services/org_jobs.py`, registered in the job registry)
- `pilot.remind {org_id, days_left}`: enqueues `org.pilot.ending {org_id, ends_at, days_left}` keyed by `org_id`; skipped if the org is no longer a pilot, or if the pilot was extended so the job is stale.
- `pilot.expire {org_id}`: a pilot whose end has passed becomes `pilot_expired` (audit actor `system`); skipped if it was converted, suspended, closed or extended. Members stay members; `join()` refuses (`ORG_INACTIVE`) and `/internal/memberships` reports `pilot_expired`.
- `org.close_members {org_id}`: ends active memberships with reason `org_closed` through `membership_service.end`, 200 per transaction (`CLOSE_BATCH_SIZE`); each batch commits alone, so after a crash the retry continues with what is left; each emits `org.member.left`.

### Request plumbing added alongside
- `known_users` is also filled from logged-in requests (`security/known_user_sync.py`): after a JWT passes, a missing row is created or a changed email updated, in its own short transaction, never for a deleted user, never setting `email_verified_at`, cached "in sync" per `(user_id, email)` for 10 minutes, and a failure is logged and never fails the request.
- `scripts/create_topics.py` (`make topics`) creates the topics this service produces and its DLQ (3 partitions, replication factor 1 by default; idempotent).

## 3c. Phase 6: the organisation API for owners and admins [built]

All routes are on the router at `/org/v1` with `route_class=IdempotentRoute` (a POST needs an `Idempotency-Key`; PATCH and DELETE do not). Every route except `/me` uses `require_org_role(<action>)`: **a non-member gets 404, a member without the right gets 403** (`FORBIDDEN_ROLE`). Every write is audited with `actor_kind = user`.

### The organisation status gate (`services/org_gate.py`, one helper used by every write)
| Status | Writes | Reads |
| --- | --- | --- |
| `closed` | every write is refused: `409 ORG_INACTIVE`, leave included (the close job ends everyone) | allowed |
| `suspended`, `pilot_expired` | role changes, removals, detail edits, transfer and leave are still allowed (an admin may tidy up) | allowed |
| `pilot`, `active` | allowed | allowed |
Reads are allowed in every status so members can see why access stopped. The system's own endings (the close job, user deletion) do not use the gate.

### Routes
| Route | Action (who) | What it does | Errors |
| --- | --- | --- | --- |
| `GET /org/v1/me` | any logged-in user | `{org: {id, name, slug, type, status, pilot_ends_at}, role, seats: {plan_code, status, used, bought, over_by}, features: {lock_modes, shared_modes_max, usage_reports, spend_cap, csv_invites, invite_links, domains}}`. `bought` is null for payg and while seats are `incomplete` or `ended`; `over_by` is 0 unless used > bought; `features` come from `core/plan_features.py` by the seat plan (no seat row means nothing paid is on). Two queries (membership + org + seat_state, and one count); timed as `org_internal_requests_seconds{route="/org/v1/me"}` | 404 not in an org |
| `GET /org/v1/orgs/{org_id}` | `VIEW_ORG` (any member) | `{id, name, slug, type, status, legal_name, gstin, state_code, billing_email, pilot_ends_at, created_at}`; no audit, no seat details | 404 |
| `PATCH /org/v1/orgs/{org_id}` | `EDIT_DETAILS` (owner, admin) | any of `name` (3-120), `legal_name`, `gstin`, `state_code`, `billing_email`. GSTIN and state-code checks are `org_service.validate_gstin_and_state` (the one function create also uses); when both are set the first two GSTIN digits must equal `state_code`. The slug never changes. Audit `org.details_changed` lists only the changed fields, old and new; nothing changed means no audit. An empty body is 422 | 403, 404, 409 `ORG_INACTIVE`, 422 |
| `GET /org/v1/orgs/{org_id}/members?cursor=&limit=&q=&role=` | `VIEW_MEMBERS` (owner, admin) | active members, oldest first, keyset on `(joined_at, id)`, `limit` default 50 max 100, opaque cursor, `q` 2-100 characters matching email or name (case-insensitive), `role` filter; rows `{user_id, name, email, role, source, joined_at}` (name and email null if the user is not in `known_users`); `next_cursor` null on the last page | 403, 404, 422 (limit, cursor, q, role) |
| `PATCH /org/v1/orgs/{org_id}/members/{user_id}` `{role}` | `CHANGE_ROLE` (owner) | `membership_service.change_role`; `admin` or `member` only. Audit `member.role_changed {from, to}`. **No Kafka event** (billing does not read the role). The same role is a no-op 200 with no audit | 403 (your own role; a non-owner), 404, 409 `ORG_INACTIVE`, 422 (set `owner`, or change an owner: use transfer) |
| `DELETE /org/v1/orgs/{org_id}/members/{user_id}` | `REMOVE_MEMBER` (owner any role; admin members only) | `ctx.check_target(target role)`, then `membership_service.end(reason=removed)` which emits `org.member.left`; answers 204 | 403, 404, 409 `ORG_INACTIVE`, 422 (yourself: use leave) |
| `POST /org/v1/orgs/{org_id}/leave` | `LEAVE` (any member) | `end(reason=left)`; 200 `{left: true}` | 404, 409 `LAST_OWNER`, 409 `ORG_INACTIVE` |
| `POST /org/v1/orgs/{org_id}/transfer-ownership` `{to_user_id}` | `TRANSFER_OWNERSHIP` (owner) | `membership_service.transfer_ownership`: the target must be an active ADMIN of this org; in one transaction the target becomes owner and the caller becomes admin; audit `member.role_changed` for both plus `org.ownership_transferred`; 200 `{owner_id, previous_owner_id, previous_owner_role}` | 403, 404, 409 `ORG_INACTIVE`, 422 (yourself, a member, a non-member, someone who left) |
| `GET /org/v1/orgs/{org_id}/audit?cursor=&limit=&action=&actor_id=` | `VIEW_AUDIT` (owner, admin) | newest first, keyset on `(at, id)`, `limit` default 50 max 100; rows `{id, action, actor_id, actor_kind, target_type, target_id, details, reason, at}`. **For the organisation's view `reason` is shown only on rows with `actor_kind = user` (staff and system reasons are internal), and `details` keys starting with `staff_` are dropped.** | 403, 404, 422 |
| `GET /admin/org/v1/orgs/{org_id}/members` and `/audit` | staff `support` or `admin` | the same service functions and pagination; the audit view shows everything (reason, `staff_` details) | 403 non-staff, 404 unknown org |

### Lock order (every use case that touches membership rows)
1. the organisation's **`seat_state` row** `FOR UPDATE` (or the `organisations` row if it has none): `membership_service.lock_org`. Join, end, change_role, transfer_ownership, remove and leave all take it first, so they queue on one row;
2. then **memberships**, locked `FOR UPDATE` in **id order** (`MembershipRepository.lock_active`): a use case that needs the caller's row and the target's row locks both together, so two of them never wait on each other in opposite orders;
3. the caller's role used for the permission check is the one read under that lock (a concurrent transfer may have changed it since the request began), carried by `OrgContext` copy with the fresh role.
`tests/postgres/test_member_concurrency.py` runs transfer and remove (and change_role) at the same time without a deadlock, and two admins removing the same member give one success and one 404.

### `membership_service.change_role(session, org_id, user_id, new_role, actor: OrgContext)`
Checks in order: the org is not closed; `new_role` is not owner (422); the caller is not the target (403); the target is an active member (404); the caller may `CHANGE_ROLE` with their current role; `ctx.check_target(target role)`; the target is not an owner (422); `new_role` differs (else no-op, no audit); `membership_rules.check_role_change`. Then sets the role and audits `member.role_changed {user_id, from, to}`.

### `membership_service.transfer_ownership(session, org_id, to_user_id, actor: OrgContext)`
Lock order as above; the caller must still be the owner; `membership_rules.plan_transfer` requires the target to be an active admin (`InvalidTransferTarget`, 422); swaps the roles; audits `member.role_changed` for both and `org.ownership_transferred {from_user_id, to_user_id}`. There is always exactly one owner afterwards.

## 3d. Phase 7: email invites [built]

### Token design (`security/tokens.py`)
The raw invite token is **never stored, logged or put in an event**. `invite_token(invite_id, token_version) = base64url_nopad(HMAC-SHA256(INVITE_TOKEN_SECRET, f"invite:{invite_id}:{token_version}"))`; the database keeps only `token_hash = sha256(token).hexdigest()` (`uq_invites_token_hash`). `INVITE_TOKEN_SECRET` must be 32+ bytes; production refuses the dev default or a short one. Resend bumps `token_version`, so the old link stops matching (404 on preview). Phase 5 owner invites use the same scheme (version 1); a pre-0007 invite with a random hash gets its derived hash at the first send or resend.

### Migration `0007_invite_tokens`
`invites.token_version` int NOT NULL DEFAULT 1 (CHECK >= 1), `resend_count` int NOT NULL DEFAULT 0 (CHECK >= 0), `last_sent_at`, `purged_at`. No email CHECK existed to relax (links already allow a NULL email). Downgrade drops the columns.

### Routes (JWT unless noted; writes use `IdempotentRoute`)
| Route | Who | Notes |
| --- | --- | --- |
| `POST /org/v1/orgs/{id}/invites {emails 1..50, role member\|admin}` | INVITE (owner/admin; only the owner may invite an admin) | bucket `invite_create` plus `INVITES_PER_ORG_PER_DAY` (UTC day, 429); org must be pilot/active (`ORG_INACTIVE`); per-email result `created`, `invalid_email`, `duplicate_in_request`, `already_member`, `in_another_org`, `open_invite_exists`; an open invite past `expires_at` is closed (`expired`) first; response `{results, seats: {used, bought, open_invites, warning}}` |
| `GET /org/v1/invites/preview?token=` | **public, no JWT, not idempotent** | bucket `invite_preview` per IP; unknown, revoked, purged, accepted, or org not pilot/active: 404 `INVITE_INVALID`; expired: 410 `INVITE_EXPIRED`; returns `{org_name, org_type, inviter_name (fallback "An organisation admin"), role, email_hint "s***@amity.edu", expires_at}` |
| `POST /org/v1/invites/accept {token}` | any logged-in user | bucket `invite_accept`; locks the invite `FOR UPDATE`; expired: 410 and the invite is closed as `expired` (committed before the error); JWT email must equal the invite email (403 `INVITE_EMAIL_MISMATCH`); the same user again gets 200 with the same body; anyone else after acceptance gets 404 `INVITE_INVALID`; then `join_service.join(source=invite)`; refusals (`SEATS_FULL`, `PILOT_FULL`, `ALREADY_IN_ORG`, `ORG_INACTIVE`, `UNKNOWN_USER`) leave the invite open; 200 `{org_id, org_name, role}` |
| `GET /org/v1/orgs/{id}/invites?status=&cursor=&limit=` | VIEW_INVITES | status is computed (open, accepted, expired, revoked), newest first, keyset `(created_at, id)`, never the hash |
| `POST /org/v1/orgs/{id}/invites/{invite_id}/resend` | INVITE | not accepted or revoked (a merely late one is revived); at most 3 (`INVITE_RESEND_MAX`), then 429 `RATE_LIMITED` with `Retry-After`; `token_version + 1`, new hash and expiry, queues `invite.send`, audit `invite.resent` |
| `DELETE /org/v1/orgs/{id}/invites/{invite_id}` | INVITE | 204; already closed is a no-op with no audit |
| `POST /admin/org/v1/orgs/{id}/invites/{invite_id}/resend {reason}` | staff | same as resend, audited as staff |

**api-gateway must treat exactly `GET /org/v1/invites/preview` as a public path** (every other route needs the JWT).

### Sending (`email/`, `services/invite_jobs.py`)
`EmailSender` port: `ConsoleEmailSender` (logs "DEV EMAIL (not sent)"; refused in production), `SmtpEmailSender` (blocking smtplib in a thread, 10 s timeout). `email/templates.py` renders subject `<inviter> invited you to <org> on GranthanAI`, plain text and HTML (everything html-escaped, subject on one line, expiry date in IST like `23 Nov 2026`, no tracking or images, "If you didn't expect this, ignore this email"). `INVITE_EMAILS_VIA=local` queues the in-process job `invite.send {invite_id, token_version}` in the creating transaction; the job derives the token at send time, skips stale versions, closed or expired invites and an org that is not pilot/active, sets `last_sent_at`, and logs neither the link nor the address. `INVITE_EMAILS_VIA=notification` queues nothing: notification-service consumes `org.invite.created` and will need an internal accept-url route (**not built**; listed in PHASES.md "Changes needed in other repos").

### Jobs (recurring, daily)
`invites.expire`: open invites past `expires_at` get `revoked_at = now`, `revoke_reason = 'expired'`, in batches of 500. `invites.purge`: never-accepted invites created more than 90 days ago get `email = NULL`, `purged_at = now`, in batches of 500.

### Audit view and joins (M1, M2)
The org audit view shows staff rows with `actor_id` null and `actor_label` "GranthanAI staff", system and service rows "GranthanAI", user rows their id and name or email. `join()` refuses a user with no `known_users` row: 422 `UNKNOWN_USER` (a deleted user stays 404).

## 4. Events and workers [P4: built]

### Envelope
Produced events are flat JSON objects (`app/events/schemas.py`, `extra="forbid"`): `event_id` (uuid7 string), `schema_version` `"1.0"`, `occurred_at` (ISO UTC with a `Z`, whole seconds), then the fields below. A test checks that `OrgMemberJoined` and `OrgMemberLeft` carry every field billing's consumer models read (`event_id, occurred_at, org_id, user_id, role, org_type`, copied into `tests/fixtures/billing_org_member_fields.json`).

| Topic | Payload fields | Key |
| --- | --- | --- |
| `org.member.joined` | `org_id, user_id, role, org_type, source` | `user_id` |
| `org.member.left` | `org_id, user_id, role, org_type, reason: left\|removed\|user_deleted\|org_closed` | `user_id` |
| `org.policy.changed` | `org_id, version, lock_modes, approved_mode_ids, member_spend_cap_paise` | `org_id` |
| `org.invite.created` | `org_id, invite_id, email, role, org_name, inviter_name, expires_at` (no token, no URL) | `org_id` |
| `org.pilot.ending` | `org_id, ends_at, days_left` | `org_id` |
| `organisation.dlq` | dead letters (below) | original key |

Always send `org_type` on member events (billing defaults a missing one to `company`, which gives a college member 60 messages a day instead of 30). Member events are keyed by `user_id` so one person's join and leave stay in order; `enqueue` refuses a key that is not the topic's key field.

### Outbox (`events/producers.py`, `workers/outbox_worker.py`)
- `enqueue(session, topic, key, event)` validates the event and inserts an outbox row in the CALLER's transaction: it never publishes and never commits, so the event exists if and only if the change commits.
- **One relay at a time.** The relay holds `pg_try_advisory_lock(7240001)` on its own connection while it relays; other instances idle and retry every 5 s (and take over within seconds if the leader dies, because its connection closes and the lock goes). Reason: per-key order. Two relays could each take a row of the same key and publish them out of order (a `left` before its `joined`), and `SKIP LOCKED` batches cannot prevent that.
- Each batch takes the oldest unpublished rows (`ORDER BY created_at, id`, 100) and publishes them in that order. When a row fails or is still backing off, every LATER row with the **same key** is skipped in this pass, while other keys keep going: per-key order is kept and one stuck key does not block the rest. A failure sets `attempts`, `last_error` and `next_attempt_at` with exponential backoff (1 s, 2 s, 4 s ... capped at 10 min); from the 10th attempt a `stuck` warning is logged (topic, key, attempts, last_error; never the payload). `published_at` is set only after the broker acks (`acks=all`, idempotent producer, 5 ms linger). Crash after an ack but before the commit means a row is sent again: at-least-once, and consumers dedupe on `event_id`.
- Metrics: `org_outbox_pending`, `org_outbox_oldest_age_seconds`, `org_outbox_blocked_keys` (gauges: keys with a failed or backing-off row in the last pass), `org_outbox_publish_failures_total` (counter).

### Consumers (`workers/consumer_worker.py`, `services/user_event_handlers.py`)
- One consumer, group `KAFKA_CONSUMER_GROUP`, topics `user-events` and `user.deleted`, `enable_auto_commit=False`, `auto_offset_reset=earliest` (a new group first catches up on the topics' history). The offset is committed only after the handler's transaction commits, or after the message went to the DLQ.
- **Dedupe:** every handler runs in one `unit_of_work` whose FIRST statement is `INSERT INTO processed_events (consumer, event_id) ON CONFLICT DO NOTHING`; a conflict means already done, so the handler is skipped and the offset committed. The dedupe row and the effects commit together. Consumers: `user_registered`, `email_verified`, `user_deleted`, and `user_events_ignored` (other `user-events` types are skipped but recorded).
- **Retries:** database and connection errors are retried after 0.5 s, 2 s and 5 s (`CONSUMER_MAX_RETRIES=3`, so four tries in all), then the message goes to the DLQ. Undecodable bytes, schema errors and handler bugs go to the DLQ at once, with no retries.
- **DLQ** (`organisation.dlq`): `{original_topic, partition, offset, key, value, value_encoding: utf-8|base64, error, attempts, failed_at}`. If the DLQ send itself fails it is retried every 5 s; on shutdown the offset is left uncommitted.
- Handlers: `UserRegistered` upserts `known_users` (email normalised) and ignores a user whose `deleted_at` is set; `EmailVerified` keeps the earliest `email_verified_at`, updates the email, ignores deleted users, then calls the hook `on_email_verified` (does nothing until Phase 9); `user.deleted` marks the user deleted (creating a `<id>@deleted.invalid` row if unknown), ends the active membership with `membership_service.end(reason=user_deleted)`, writes `org.owner_missing` if that left the org without an owner (it never blocks), and revokes open invites to the user's email (`revoke_reason='user_deleted'`), all in one transaction.

### `membership_service.end(session, org_id, user_id, reason, actor)` (pulled forward from P5c)
In the caller's transaction: (1) load the active membership, else `NotFound`; (2) lock the org's `seat_state` row (the row every join locks; an org without one is locked through `organisations`); (3) count active owners and run `membership_rules.check_can_end`; (4) set `status` (`left` for reason `left`, otherwise `removed`), `ended_at`, `end_reason`; (5) audit `member.left` or `member.removed`; (6) enqueue `org.member.left` keyed by `user_id` with `org_type`; (7) return `EndResult(owner_missing)`. Phase 5 reuses it unchanged. There is no `join()` yet.

### Job runner (`workers/job_runner.py`, `services/job_service.py`, `services/job_handlers.py`)
- Claims due jobs (`run_at <= now`, not done, `locked_until` null or past) with `FOR UPDATE SKIP LOCKED`, sets `locked_until = now + 5 min`, and runs the handler registered for the `kind` in its own transaction (effects and `done_at` commit together).
- Failure: `attempts + 1`, `last_error`, `run_at` pushed back with exponential backoff. After 8 attempts the job is closed with `last_error` starting `gave up` and counted in `org_jobs_failed_total{kind}`.
- `schedule(session, kind, run_at, payload, dedupe_key=None)`; `ensure_recurring(session, kind, every, clock)` keeps ONE open job per kind (`dedupe_key = "recurring:<kind>"`, interval in the payload; the runner schedules the next run after each run, including after a give-up). Startup calls it, so it is idempotent.
- Registered jobs: `idempotency.cleanup` (hourly: delete `idempotency_keys` older than 24 h), `processed_events.cleanup` (daily: older than 30 days), `outbox.cleanup` (daily: published more than 7 days ago; unpublished rows are never deleted). Phase 5+ adds pilot, invite and domain jobs to the same registry.

### Workers at startup (`workers/manager.py`)
With `WORKERS_ENABLED=true`, `main.py` starts the outbox relay, the consumer worker and the job runner as supervised asyncio tasks. A crash is logged and the worker restarts after 5 s; the API is never affected and `/ready` does not depend on workers (`org_worker_up{worker}` gauge shows 1/0). On shutdown the stop event is set, workers get 10 s, then they are cancelled and the Kafka clients closed.

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
- `require_org_role(action)`: reads `org_id` from the path, loads the caller's ACTIVE membership, runs `permission_rules.check`; not a member (never joined, left, removed) is 404 `NOT_FOUND`, never 403. Returns `OrgContext {org_id, user, role, membership_id}`. An `OwnRoleOnly` cell passes the dependency; the use case must call `ctx.check_target(target_role)` (`OrgContext.check_target` runs `permission_rules.check(action, role, target_role)`): Phases 5-6 call it for remove and role changes. Staff do not use it.
- **Services:** `INTERNAL_SERVICE_TOKENS` (`name:token,...`) is parsed once at startup (empty names, empty tokens, duplicate names and shared tokens refuse to boot); `require_service()` compares the bearer with every token using `hmac.compare_digest` without returning early; a miss is 401 and writes nothing. billing's `ORG_SERVICE_TOKEN` must equal the `billing:` entry.
- Nothing logs the Authorization header, a raw token or a jti.

### Idempotency (`IdempotentRoute`, `services/idempotency_service.py`)
Public and admin routers use `APIRouter(route_class=IdempotentRoute)`. Every POST needs `Idempotency-Key` (8-128 of `[A-Za-z0-9_-]`, else 422). Scope `(actor_id, route template, key)`; `request_hash` = sha256 of the canonical JSON body.
1. **Reserve** `state='in_progress'`, `locked_until = now + 60 s`, in its own short transaction.
2. **On conflict**, read the row: hash differs gives 422 `IDEMPOTENCY_MISMATCH`; `done` replays the stored status and body with `Idempotent-Replayed: true`; `in_progress` and locked gives 409 `IDEMPOTENCY_IN_PROGRESS` with `Retry-After`; lock passed (or the row is older than 24 h) is taken over atomically.
3. **After the handler:** only 2xx is stored (`state='done'`). Every 4xx and 5xx, and any exception, deletes the reservation so the client can retry (`IDEMPOTENCY_MISMATCH` and `IDEMPOTENCY_IN_PROGRESS` are answered before the handler and never stored).

Before anything is reserved or replayed, the route class authenticates the caller with the same helper as `get_current_user` (`authenticate_user`: JWT, blacklisted jti, deleted user, Redis down on a write gives 503), so a revoked token or deleted user never reads a stored response.
Cleanup: the `idempotency.cleanup` job (Phase 4, hourly) deletes rows older than 24 h; rows past 24 h are also ignored on read and taken over.

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

See `.env.example` for every setting with its comment: service (`ENVIRONMENT`, `SERVICE_PORT=8010`, `DATABASE_URL`, `DB_*`, `REDIS_URL`, `KAFKA_*`), identity (`JWT_*`, `INTERNAL_SERVICE_TOKENS`), billing (`BILLING_BASE_URL`, `BILLING_SERVICE_TOKEN`), `WORKERS_ENABLED`, email (`EMAIL_SENDER`, `SMTP_*`, `APP_BASE_URL`), invites (`INVITE_TTL_DAYS`, `LINK_MAX_TTL_DAYS`, `INVITES_PER_ORG_PER_DAY`, `INVITE_TOKEN_SECRET`, `INVITE_EMAILS_VIA`, `CSV_MAX_*`), pilots and seats (`PILOT_MAX_MEMBERS`, `PILOT_DAYS`, `SEAT_SOURCE`), `DOMAIN_AUTO_JOIN_ENABLED`. Production refuses dev defaults (`Settings._production_problems`).

## 9. Defaults D1-D6

D1 one active org per user (partial unique index). D2 joins hard-stop when full. D3 owners and admins count as seats. D4 only staff create organisations in v1. D5 domain auto-join off unless `DOMAIN_AUTO_JOIN_ENABLED=true`. D6 org admins see names, emails and usage counts, never content.
