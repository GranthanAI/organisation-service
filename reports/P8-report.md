# P8: organisation-service, report

Part A fixed N1 (global email send rate). Part B is Phase 8: CSV bulk invites and invite links, built to your design change (validate and create in the upload request, only the emails in the background).

## Gap fix N1: done

- `EMAIL_SENDS_PER_MINUTE` (default 60; `.env.example` says to match the SMTP provider's limit). `invite.send` takes a token from the Redis counter `org:email_send:<minute>` (`services/email_throttle.py`) after it has decided the invite is still sendable, so skipped invites use no slot.
- A full minute raises `DeferJob(next minute)`: the runner moves `run_at`, leaves `attempts` and `last_error` alone, and records no failure (the "defer" result you asked for).
- Redis down: this process sends at most one email a second (warning logged); the rest defer by a second.
- Metrics `org_email_sent_total`, `org_email_deferred_total`.
- Tests (`tests/postgres/test_email_throttle.py`, 5): 61 queued sends give 60 sent and 1 deferred to the next minute, which then sends; a deferral adds no attempt; a skipped invite takes no slot; Redis-down fallback; key and expiry. The tests' Redis is an autouse fake in `tests/conftest.py`.

## Phase 8 as built

- **Migration `0008_csv_and_links`** (up, down, up tested): `invites.invitee_name`, `invite_batches.dry_run`. **EXPLAIN on 42,000 invites: no new index needed.** Batch progress uses a bitmap scan on `ix_invites_batch`; `csv.batches.finish` is a nested-loop anti join on `ix_invites_batch`; the daily CSV count uses `ix_invites_org_created`; the open-email lookup is an index-only scan on `ux_invites_open_email`.
- **Upload** `POST /org/v1/orgs/{id}/invites/csv`: file rules, row reasons in your order with spreadsheet row numbers, `over_seats` (paid, pilot with `PILOT_MAX_MEMBERS`, payg, incomplete/ended), `CSV_ROWS_PER_ORG_PER_DAY`, dry run, bulk insert with ON CONFLICT DO NOTHING, one `org.invite.created` and one `invite.send` per invite, one audit row. All in one transaction.
- **Batches:** status, `errors.csv` (formula-safe, BOM, attachment name), `csv.batches.finish` every minute, `invites.purge` clears CSV names and error rows older than 90 days.
- **Links:** create, list (no URL), URL again (audited), revoke; accepting a link (no email binding, domain restriction, `max_uses`, same-user retry); link preview.
- `app/utils/csv_safe.py` did not exist; I created it.

## Bug found and fixed on the way (affects Phase 7 too)

`InviteRepository.get_for_update` locked the row but SQLAlchemy kept the copy loaded earlier by `accept` (looked up by token), so `uses` was stale. The 10-concurrent-accepts link test admitted more than `max_uses`. The same stale read applied to email invites. Fixed with `populate_existing` on the locked read; the concurrency test now admits exactly 5.

## Commits (range `0e51041..HEAD` is P7; Phase 8 starts at `7a544d5`, no trailer, not pushed)

| Hash | Message |
| --- | --- |
| `7a544d5`, `acfdff8` | P8-fix-N1: global email send rate; lint |
| `f524df0` | P8a: CSV bulk invites validated and created in one request |
| `05b0b7c` | P8b-P8c: batch progress, finish job, batch status and errors.csv, purge |
| `aa66721` | P8d-P8e: invite links and accepting a link; invite row re-read under lock |
| next | P8: lld and PHASES for the design change |
| next | P8: batch access check in the service layer (layering test) |

The report is committed after these (`P8f: P8 report`).

## Tests (one file at a time, final run at the end; ruff clean; no `org_test_*` databases left)

| File | Result |
| --- | --- |
| tests/postgres/test_email_throttle.py (new) | 5 passed |
| tests/postgres/test_migrations.py | 12 passed (0008 up/down/up added) |
| tests/unit/test_csv_parse.py (new) | 29 passed (also covers `csv_safe`) |
| tests/api/test_csv_upload.py (new) | 26 passed |
| tests/postgres/test_csv_batch.py (new) | 6 passed (2,000 rows: 1,950 and 50) |
| tests/api/test_batch_status.py (new) | 8 passed (includes the 5 s check) |
| tests/api/test_invite_links.py (new) | 33 passed |
| tests/postgres/test_invite_jobs.py | 11 passed (purge of names and batch errors added) |
| tests/postgres/test_job_runner.py | 19 passed (recurring list now has `csv.batches.finish`) |
| tests/api/test_invite_accept.py | 24 passed (race test accepts 403 or 404) |
| tests/postgres/test_repositories.py | 12 passed (single-invite count excludes links) |
| tests/unit/test_layering.py | 59 passed (batch access moved to the service) |
| tests/api/: admin_members 7, admin_orgs 79, audit_api 16, current_user 10, health 12, internal_routes 26, invite_preview 22, invites_create 31, invites_manage 18, known_user_sync 10, me 21, members 56, org_details 42, problem_json 42, require_org_role 20, service_tokens 22 | all passed |
| tests/integration/test_kafka_roundtrip.py | 3 passed |
| tests/postgres/: audit_guard 8, audit_service 21, consumer_worker 18, db_errors 25, idempotency 30, invite_send_job 13, join_service 39, member_concurrency 14, membership_service 20, one_active_org 7, outbox_worker 27, pilot_jobs 17, seat_lock 4, seat_race 5, seat_service 37, unit_of_work 7, user_event_handlers 19 | all passed |
| tests/unit/: config 30, create_topics 6, email_rules 73, email_sender 16, event_schemas 29, exceptions 16, ids 8, invite_templates 11, invite_tokens 8, jwt 23, membership_rules 35, org_lifecycle_rules 45, permission_rules 142, rate_limit 21, seat_rules 28, staff_roles 18, tokens 4, worker_manager 8 | all passed |

The sweep run flagged `test_repositories` and `test_layering`; both were fixed afterwards and re-run (above).

## Exit check

- **`college_2000.csv` uploads through the API in 3.55 s** (4.22 s on the first, slower, version; measured in `test_batch_status.py` against the shared Postgres on this machine; the limit is 5 s). 2,000 rows give 1,950 invites and 50 errors. The speed-up came from inserting with executemany instead of a 2,000-row VALUES list (about 2 s of statement compiling).
- **Error CSV:** checked in the test: no cell starts with `=`, `+`, `-`, `@`, tab or CR; dangerous cells start with `'`; BOM present; `row,email,reason`; `attachment; filename="invite-errors-<id>.csv"`.
- **Live check** (dev stack, `WORKERS_ENABLED=true`, console sender, `EMAIL_SENDS_PER_MINUTE=30`):
  1. Owner uploaded a 40-row CSV with 3 bad rows: 200, 37 created, 3 errors (2 invalid, 1 duplicate). The log shows **30 DEV EMAILs in the first minute and 7 in the next**; the batch went `sending` to `done`; `errors.csv` has the 3 rows.
  2. Link with `max_uses` 2: two dev-JWT users accepted (200, member), the third got **404 `INVITE_INVALID`**. `org.member.joined` with `source: "link"` and `org_type: "college"` was read back from the topic for both.
  - One of my status polls hit a connection reset once (a Kafka group rebalance was in the log); the server stayed up and the next calls worked.

## Decisions needed

- Whole-upload `role=admin` by a non-owner is 403 `FORBIDDEN_ROLE`; an admin row inside the file from a non-owner is `role_not_allowed` (your prompt said FORBIDDEN_ROLE "for those rows", and the reason list has `role_not_allowed`).
- A row with an unknown role value (for example `owner`) is `role_not_allowed`; a blank role cell uses the form role.
- A dry run also checks the daily row limit, so it predicts the real answer (429).
- `over_seats` counts open email and CSV invites but not links (a link holds no seat). `GET .../invites` no longer lists links.
- The daily single-invite limit now counts email invites only (CSV rows and links have their own).
- Same-user link retry: an active membership in that org with source `link` gets 200, even if the link has filled up since. I added no column.
- A link row is closed by `invites.expire` like any invite (`expired`); `used_up` is computed.
- In notification mode (`INVITE_EMAILS_VIA=notification`) a CSV batch is `done` at once, because nothing is sent from here.
- A batch stays `sending` while a live invite of it is unsent (for example the org is suspended); there is no timeout.
- Multipart bodies are fingerprinted with the boundary replaced, so retrying an upload with the same `Idempotency-Key` replays instead of failing with `IDEMPOTENCY_MISMATCH`.
- No request-size limit is enforced before the body is read (the Idempotency wrapper reads it); the file is capped at `CSV_MAX_BYTES + 1` when parsed. api-gateway should cap request bodies.

## Not done

- No domains, join requests, policies or usage report (Phases 9 and later).
- Changes needed in other repos: unchanged from P7 (api-gateway public preview path, notification-service and its accept-link route, frontend `/join/<token>` and the upload, batch and link pages); api-gateway should also limit upload body size.
