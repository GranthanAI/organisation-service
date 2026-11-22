# Runbook

For whoever runs organisation-service. Every step names the exact route or command. `$ORG` is the organisation id, `$STAFF` a staff
JWT (roles `admin`, `billing_admin`, `support`; `support` can only read), `$OWNER` the organisation owner's JWT. All routes are under
the gateway: `/org/v1/...` for organisation people, `/admin/org/v1/...` for staff. Every POST, PUT, PATCH and DELETE needs an
`Idempotency-Key` header (any unique string; repeating a request with the same key replays the first answer).

Dev shortcuts: `uv run python -m scripts.make_staff_token` prints a staff JWT (development only); `scripts.read_topic <topic>` reads
a Kafka topic; `make dev` runs the API on `127.0.0.1:8010`.

## Run it

- `make setup`, `make infra-up`, `make db-create`, `make db-migrate`, `make topics`, then `make dev` (or, in production, `uvicorn app.main:app --host 0.0.0.0 --port 8010` per replica). Migrations are run by `alembic upgrade head`, never at startup.
- Liveness `GET /live`; readiness `GET /ready` (503 only when Postgres is down; Redis and Kafka show as DEGRADED); metrics `GET /metrics`.
- Settings: README.md lists every one. Production refuses to start with a dev default (`Settings._production_problems`).

## Create a college pilot

1. Staff: `POST /admin/org/v1/orgs` with `{"name": "Amity University", "type": "college", "billing_email": "accounts@amity.edu", "owner_email": "dean@amity.edu", "pilot": true, "reason": "pilot agreed with the registrar"}` (add `legal_name`, `gstin`, `state_code` when known; the GSTIN and state code must match).
2. If the owner already has an account the response says `owner_joined: true`. Otherwise `owner_invite_pending: true`: an email with a join link goes to `owner_email` (re-send: "Re-send failed invites" below).
3. The pilot is 25 members for `PILOT_DAYS` (30). Reminders go at 7 and 1 days before the end (`org.pilot.ending`).

## CSV-invite the students

1. The owner or an admin: `POST /org/v1/orgs/$ORG/invites/csv` (multipart: `file`, optional `role=member`, `dry_run=true`). Always run it once with `dry_run=true`: nothing is written except the report.
2. Fix the rows listed in `errors_preview` (the whole list: `GET /org/v1/invite-batches/$BATCH/errors.csv`). Reasons: `invalid_email`, `duplicate_in_file`, `role_not_allowed`, `already_member`, `in_another_org`, `open_invite_exists`, `over_seats`.
3. Upload for real (same call without `dry_run`). The invites exist at once; emails go out in the background at `EMAIL_SENDS_PER_MINUTE`. Progress: `GET /org/v1/invite-batches/$BATCH` (`sent`, `pending_send`, `not_sent`, `status` sending, done or stopped).
4. A pilot allows 25 members: rows beyond the free seats come back as `over_seats`.

## Convert a pilot to org_pro

With `SEAT_SOURCE=staff` (today): 1. Staff set up the subscription in billing first (`/admin/billing/v1`: `orgs/{id}/subscriptions`, then `orgs/{id}/seats`, then `invoices/{id}/mark-paid` for a bank transfer). 2. Then here: `POST /admin/org/v1/orgs/$ORG/seats` with `{"plan_code": "org_pro", "variant": "college", "seats_bought": 200, "reason": "invoice 1042 paid"}` (a college needs at least 50 seats). The pilot becomes an active organisation and the cap lifts.
With `SEAT_SOURCE=billing` (once billing has `GET /internal/orgs/{id}/subscription`): do step 1 only. billing's `subscription.changed` makes this service fetch the subscription; the nightly `seats.refresh_all` catches a lost event. Staff seat writes then answer `SEATS_MANAGED_BY_BILLING`. Until billing builds the route, the service logs "has no ... subscription route yet" once and keeps the seats it has.
Check: `GET /org/v1/me` as the owner shows `seats {plan_code, status, used, bought, over_by}`.

## Fix an org with no owner

The audit log shows `org.owner_missing` (the last owner's account was deleted). Staff: `POST /admin/org/v1/orgs/$ORG/members` with `{"user_id": "<the new owner's id>", "role": "owner", "reason": "ownership confirmed by the registrar"}`. The user must already have an account (UNKNOWN_USER otherwise) and be in no organisation. If the person is already an admin or member of this organisation there is no route that promotes them (nobody here may change roles without an owner): have them leave (`POST /org/v1/orgs/$ORG/leave`) and add them again as owner. Once there is an owner, later changes are `POST /org/v1/orgs/$ORG/transfer-ownership`.

## Re-send failed invites

- One invite (the owner or an admin): `POST /org/v1/orgs/$ORG/invites/$INVITE/resend` (a new link, the old one dies, at most 3 times). Staff: `POST /admin/org/v1/orgs/$ORG/invites/$INVITE/resend` with a `reason`.
- Emails that failed to send: the job `invite.send` retries 8 times with backoff, then gives up (`org_jobs_failed_total`). After fixing the cause (SMTP credentials, provider outage) requeue the ones that gave up:
  `UPDATE jobs SET done_at = NULL, attempts = 0, last_error = NULL, run_at = now(), locked_until = NULL WHERE kind = 'invite.send' AND last_error LIKE 'gave up%';`
- Many invites made while `INVITE_EMAILS_VIA=notification` was set and notification-service was down: switch back to `local` and resend, or wait for notification-service.

## Replay the DLQ

Dead letters are on `organisation.dlq` (`org_dlq_messages_total` counts them). 1. Read them: `uv run python -m scripts.read_topic organisation.dlq`. 2. Fix the cause (the `error` field says what failed). 3. `uv run python -m scripts.replay_dlq --dry-run`, then `uv run python -m scripts.replay_dlq` (or `--contains <user id>`). The consumer deduplicates by event id, so a replay can never apply a message twice.

## Rotate secrets

- `INTERNAL_SERVICE_TOKENS` (`name:token,name:token`): add the NEW token as a second entry for the same caller under a new name (for example `billing:OLD,billing2:NEW`), restart, give billing the new token (`ORG_SERVICE_TOKEN`), confirm `GET /internal/ping` works with it, then remove the old entry and restart. Tokens are at least 32 characters in production.
- `BILLING_SERVICE_TOKEN` is billing's token for us: change it in both places together; a mismatch shows as 503 on usage reports with "billing refused our service token" in the log.
- `INVITE_TOKEN_SECRET`: **rotating it kills every outstanding invite and link** (their tokens are derived from it). Rotate only if it leaked. Afterwards: bulk-resend open email and CSV invites (resend route, or a fresh CSV upload), and re-issue invite links (`POST .../invite-links`; old ones answer `INVITE_INVALID`). It must be at least 32 bytes.
- `JWT_SECRET_KEY` must match auth-service's; rotate both together.

## Verify a domain by hand

Staff: `POST /admin/org/v1/orgs/$ORG/domains/amity.edu/manual-verify` with `{"reason": "college IT is slow; confirmed with the registrar by phone"}`. The domain must already be added by the owner (`POST /org/v1/orgs/$ORG/domains`) and not verified by another organisation. A hand-verified domain is never re-checked against DNS. The owner then turns auto-join on (`PATCH /org/v1/orgs/$ORG/domains/amity.edu {"auto_join": true}`) when `DOMAIN_AUTO_JOIN_ENABLED=true`.

## Read the alerts

`docs/alerts.md` lists each alert with its metric, threshold and the section below. The first look is always `GET /ready`, `GET /metrics` and the log (every 4xx and 5xx is one `request failed` line with `code`, `route`, `status` and `correlation_id`; search the id).

### Outbox backlog
`org_outbox_oldest_age_seconds` high: events are not reaching Kafka. Check Kafka (`/ready` shows `kafka`), the log for "publish failed", and that the relay worker is up (`org_worker_up{worker="worker-outbox"}`). The relay publishes in order per key; one key that keeps failing blocks only itself (next section). Nothing is lost: rows stay in the `outbox` table and go out when Kafka is back.

### Blocked outbox keys
`org_outbox_blocked_keys > 0` for 15 minutes: one entity's events keep failing to publish (a bad payload, or a message Kafka refuses). `SELECT key, topic, attempts, last_error FROM outbox WHERE published_at IS NULL ORDER BY created_at LIMIT 20;` shows which. Fix the cause; the relay retries with backoff.

### Consumer lag
`org_consumer_lag` high: user-events and user.deleted (and subscription.changed in billing mode) are not being applied. Check that `worker-consumer` is up, the database is reachable, and the DLQ is not growing. A new member who just verified their email may not yet be a member until the lag clears.

### Slow internal lookups
billing waits on `GET /internal/memberships/{id}`. Check Postgres latency and connections (lld.md section 10), the event loop alert, a running CSV upload, and replicas (`DB_POOL_SIZE` x replicas against Postgres `max_connections`). billing caches answers for 60 s and falls back to the personal plan if we are down.

### Event loop lag
`org_event_loop_lag_seconds > 0.5`: something is doing CPU work on the event loop. Look at what ran (a big CSV upload is chunked and threaded; a new code path that is not will show). Restart is safe: nothing is held in memory that the database does not have.

### Failed jobs
`org_jobs_failed_total` rose: a job gave up after 8 attempts. `SELECT kind, last_error, payload FROM jobs WHERE last_error LIKE 'gave up%' ORDER BY run_at DESC LIMIT 20;`. `invite.send`: see "Re-send failed invites". Recurring jobs always schedule their next run, so housekeeping never stops.

### Email is deferred
`org_email_deferred_total` rate high: the global send limit (`EMAIL_SENDS_PER_MINUTE`, default 60) is holding mail back. Normal during a big CSV upload (a 2,000-student file takes about 33 minutes at 60 a minute). If your SMTP provider allows more, raise the limit; it is a floor on delivery speed, not an error.

### Billing errors
`org_billing_requests_seconds` with non-2xx results: usage reports (and seat refreshes in billing mode) are failing. Look at the `result` label: `5xx` and `timeout` are billing's side; `4xx` often means a wrong `BILLING_SERVICE_TOKEN` (the log says "billing refused our service token"). Users see 503 "billing-service is not available" and can retry.

### Postgres is not ready
`org_dependency_up{dependency="postgres"} == 0` or `/ready` is 503. Nothing can be served correctly without it: check the database host, credentials (`DATABASE_URL`), connection limit (too many replicas), and disk. The service recovers by itself when the database is back (the connection pool replaces dead connections).

### A worker is down
`org_worker_up == 0`: the outbox relay, the consumer or the job runner stopped (the supervisor restarts them with backoff and logs the reason). If it stays down, read the log line "worker crashed" and restart the service.
