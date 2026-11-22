# P10: organisation-service, report

The last phase. Part 0 finished the Phase 9 report, Part A fixed Q1 (latency) and Q2 (`past_due` seats), Part B is Phase 10: policies, the usage report, seats from billing, observability, load tests, the runbook and the launch checklist. One real bug turned up on the way (pool starvation, below).

## Part 0

`reports/P9-report.md` is committed (`e377919`). Its sweep was rerun on an isolated checkout of the Phase 9 commit; four test failures it found (the recurring-job list, a hook spy, the rate-limit bucket list, and a timing-sensitive event-loop test) were fixed or explained there.

## Gap fixes

- **Q1 done, measured, partly met.** Sequential p99 went from 38 ms to about 20 ms (target 20 ms: met on most runs, up to 27 ms on a busy machine). **The 20-concurrent p95 target (100 ms) is not met and cannot be met on this machine**: a route that touches no database (`/internal/ping`) is as slow, so the target is set as a multiple of that floor (lld.md section 10).
  - Where the time went (`tests/load/measure_q1.py`, in-process): raw asyncpg `SELECT 1` round trip **2.4-2.8 ms** (4-5 ms at concurrency 20); the membership query alone 2.5 ms. Through SQLAlchemy the same query was **15-17 ms** (134-152 ms at concurrency 20): a ping per checkout (`pool_pre_ping`), `BEGIN`, the query, two `ROLLBACK`s, and a pool of 5+5 that 20 callers queued for. The SQL itself was already one join.
  - Changes: pool 10+10, no pre-ping with `pool_recycle` 1800 and one retry on a dropped connection (reads only), AUTOCOMMIT sessions for `/internal` reads, the internal router mounted first, `CorrelationIDMiddleware` as plain ASGI (not `BaseHTTPMiddleware`). Direct cost per call 7.9 ms to 6.0 ms.
  - Real HTTP against uvicorn, 2,000 calls (`tests/load/internal_latency.py`):

| | c=1 p50 / p95 / p99 | c=20 p50 / p95 / p99 |
| --- | --- | --- |
| Before (HEAD of Phase 9) | 19.9 / 28.3 / 38.5 ms; again 19.6 / 27.6 / 39.7 | 238 / 755 / 1225 ms; again 207 / 672 / 1008 |
| After the pool, autocommit and no-ping changes | 10.9 / 17.1 / 20.6 ms | 172 / 594 / 1006 ms |
| After route order and plain ASGI middleware (3 runs) | 9.7-10.4 / 15-21 / 18-31 ms | 157-168 / 516-605 / 810-916 ms |
| `/internal/ping`, no database (the floor) | 7.5 / 11.1 / 13.6 ms | 138 / 480 / 725 ms |
| Last run, later, on a busier machine: membership | 12.3 / 19.3 / 26.8 ms | 201 / 664 / 936 ms |
| Last run: ping floor | 8.5 / 11.7 / 14.8 ms | 195 / 718 / 1146 ms |

  The membership lookup adds about 3 ms to the floor (c=20 p95 1.26 times the floor in the three-run series, 0.93 in the last run). The numbers vary by 20-30% between runs on this Windows machine.
- **Q2 done.** `active` and `cancelling` count as seats; `past_due` keeps members but refuses new joins with 409 `PAYMENT_DUE` (in the one code map); `/me` shows `seats.status`; `incomplete` and `ended` as before. `seat_rules.can_join(payment_due=)` stays pure; tests for every status in `test_seat_rules.py`, `test_join_service.py` (47) and `test_me.py`.

## Bug found by the load test (fixed)

The first run of the 100-way seat race gave **100 x 500** after 33 s. Every request held one database connection (the dependency session used for authentication) and waited for a second one (its own transaction), so 100 concurrent requests starved the pool of 20. Fix: authentication and the role lookup release their connection before the route opens its own (nothing holds two), an expired invite is closed in a separate transaction after the first rolled back, `DB_POOL_TIMEOUT_SECONDS` (10) and an exhausted pool answers 503 `UPSTREAM_UNAVAILABLE` with `Retry-After: 1`. Regression test `tests/postgres/test_pool_starvation.py` (a pool of 4, 40 concurrent accepts): it fails on the old code (503s) and passes now. This would have hit production on any burst of about 10 simultaneous writes.

## Phase 10 as built

- **P10a Policies:** `GET`/`PUT /org/v1/orgs/{id}/policies`, ETag and `If-Match` (428 missing, 412 stale with `current_version`), plan gating (403 naming `lock_modes` or `spend_cap`), `lock_modes` needs an approved id, cap 100-10,000,000 paise, closed org refused; one transaction writes the version, audit `policy.changed {old,new}` and `org.policy.changed` (full policy, keyed by org id).
- **P10b Usage report:** `clients/billing_client.py` (10 s, one retry, every failure 503, a 401 logged as our misconfiguration), JSON and CSV (formula-safe, BOM, filename, built in a thread), IST date rules, plan gating; contract fixture `tests/fixtures/billing_org_usage.json` copied from billing's code.
- **P10c Seats from billing:** `SeatSource` port with `StaffSeatSource` and `BillingSeatSource`; refresh upserts with source `billing`, a 404 for the organisation ends the seats, a 404 for the route warns once and changes nothing; `subscription.changed` (org owners only, deduplicated, only a hint) is consumed when `SEAT_SOURCE=billing`; nightly `seats.refresh_all`, 50 at a time; staff writes refused in billing mode. billing's `owner_type` field marks organisations (read from `adapters/kafka/schemas.py`).
- **P10d Observability:** all the listed metrics exist (`org_joins_total`, `org_seat_full_total`, `org_invites_created_total`, `org_consumer_lag`, `org_dlq_messages_total`, `org_billing_requests_seconds`, `org_dependency_up` added); every 4xx and 5xx is one `request failed` log line (code, route template, status, correlation id; no bodies, tokens or emails); `docs/alerts.md` (11 alerts) guarded by `tests/unit/test_alert_doc.py`.
- **P10e Load tests** (`tests/load/`, scripts run in the background against the dev server): `seat_race.py`, `internal_latency.py`, `csv_under_load.py`, `pilot_end_to_end.py`, `measure_q1.py`.
- **P10f Docs:** `docs/runbook.md` (each step with the exact route or command; sections the alerts point to), `docs/launch-checklist.md`, README (status, run, ports, every setting), lld.md (sections 9b and 10), the final list of other-repo changes in PHASES.md. New script `scripts/replay_dlq.py`.

## Commits (`887e50e..HEAD`, no trailer)

| Hash | Message |
| --- | --- |
| `f3886a5` | P10-fix-Q2: past_due seats keep members but refuse new joins (PAYMENT_DUE) |
| `e377919` | P9f: P9 report and test fixes; P10a policies |
| `dbe0d87` | P10-fix-Q1: measured and cut the membership lookup |
| `a0adb62` | P10b: usage report |
| `785e998` | P10c: seats from billing |
| `a5222c7` | P10d: metrics, one log line per 4xx/5xx, alerts, runbook, DLQ replay |
| `6a2b8b0`, `7bdbfa5` | P10e: load scripts; pool starvation fix |
| `e6f689e` | P10f: launch checklist, other-repo list, lld |
| `6007f72` | P10e: pilot end-to-end script |
| `5a483e4` | usage route no longer imports a repository (layering) |

Everything up to `6007f72` is pushed; the layering fix (`5a483e4`) and this report are local.

## Tests (final sweep, one file at a time; 78 files, 1,782 tests)

| Area | Result |
| --- | --- |
| New: tests/api: policies 35, usage_report 25, observability 12; tests/postgres: billing_seat_source 26, db_pool 7, pool_starvation 4; tests/unit: alert_doc 5 | all passed |
| Touched for Q1/Q2/Phase 10: tests/unit: seat_rules 30, layering 64, rate_limit 21, event_schemas 29; tests/postgres: join_service 47, job_runner 19, audit_service 21, user_event_handlers 19, repositories 12, event_loop_lag 1; tests/api: me 22, problem_json 45, internal_routes 26, known_user_sync 10, current_user 10, require_org_role 20, invite_accept 24 | all passed |
| tests/api, the rest: admin_members 7, admin_orgs 79, audit_api 16, batch_status 8, csv_upload 26, domains_api 35, health 12, invite_links 36, invite_preview 22, invites_create 31, invites_manage 18, join_requests 16, members 56, org_details 42, request_size 10, service_tokens 22 | all passed |
| tests/integration: kafka_roundtrip 3 | passed |
| tests/postgres, the rest: audit_guard 8, auto_join 18, consumer_worker 18, csv_batch 8, db_errors 25, domain_verify 15, email_throttle 5, idempotency 30, invite_jobs 11, invite_send_job 13, member_concurrency 14, membership_service 20, migrations 13, one_active_org 7, outbox_worker 27, pilot_jobs 17, seat_lock 4, seat_race 5, seat_service 37, unit_of_work 7 | all passed |
| tests/unit, the rest: config 30, create_topics 6, csv_parse 29, domain_rules 36, email_rules 73, email_sender 16, exceptions 16, ids 8, invite_templates 11, invite_tokens 8, jwt 23, membership_rules 35, org_lifecycle_rules 45, permission_rules 142, staff_roles 18, tokens 4, worker_manager 8 | all passed |
| tests/unit/test_layering.py | **1 failed in the sweep** (`api/v1/usage.py` imported a repository); fixed (64 passed) |

Ruff is clean; no `org_test_*` database is left.

## Load test results

- **Seat race** (100 users, 100 email invites, 5 free seats, all at once over HTTP): **exactly 5 got 200, 95 got `SEATS_FULL`, 5 `org.member.joined` events, 6 active members: PASS.** All 100 answered in 6.0-6.7 s (a single process on this machine). The first run, before the pool fix, was 100 x 500.
- **Internal latency:** see the Q1 table.
- **CSV under load** (`college_2000.csv` upload while 20 callers call `/internal/memberships`): the upload took 5.4 s, then 8.0 s on a second, busier run (1,950 invites and 50 errors each time). Callers' p95: 851 ms before the upload and 1,372 ms during (first run); 1,401 ms before and 1,667 ms during (second run), so the upload adds about 20-60% to the p95 while 20 callers are already saturating a single process. Absolute numbers are inflated by the 20-caller saturation described in Q1.
- **Pilot end to end** (`pilot_end_to_end.py`): staff create the college pilot (owner joined); the owner uploads a CSV of 20; 20 accepts with dev JWTs all 200; `/internal/memberships/{id}` shows all 20 `active`, `org_type: college`; the owner sees 21 of 25 seats; a pilot is refused the usage report (`FEATURE_NOT_IN_PLAN`); staff move it to `org_pro`: PASS.

## Exit check

1. Seat race admits exactly 5: **done**.
2. Internal latency: sequential p99 about 20 ms (met on most runs); the 20-concurrent p95 is set as a multiple of the no-database floor, and met in the three-run series (1.26 times) and in the last run (0.93 times). The absolute 100 ms target is not met on this machine (the floor itself is 480-720 ms p95 here).
3. College pilot end to end: **done** (above).
4. Usage report: **works against a fake billing** (25 tests, including the real-shaped fixture, JSON, CSV and every failure mode). **Against the real dev billing it could not be checked: billing was not running (port 8090 refused the connection)**, so the live call answered 503 `UPSTREAM_UNAVAILABLE` after its retry, which is the designed behaviour.
5. The full list of other-repo changes is in PHASES.md (15 rows, with status).
6. **Billing granting the org plan to one of the 20: not checked**, because billing is not running here and I did not read its secrets to configure it.
7. **Live:** a policy PUT with `If-Match` emitted `org.policy.changed` and I read it back from the topic (version 2, `lock_modes` true, two mode ids); a stale `If-Match` gave 412 with the current version and a missing one 428. With `SEAT_SOURCE=billing` and billing's route answering a plain 404, the service logged "billing-service has no /internal/orgs/{id}/subscription route yet" **once** for two `subscription.changed` events (a third, for a user, was ignored). **That route answer came from a small stub server I wrote (it returns billing's framework 404), not from real billing.**

## Decisions needed

- The Q1 target: absolute 20-concurrent p95 under 100 ms is not achievable on this machine; I used a multiple of the no-database floor. Say if you want a different rule.
- `seat_state.status = suspended` (billing's) still counts as seats, as before; only `past_due` refuses joins.
- CSV upload and single invites are not refused for a `past_due` org (the accept is); a college could create invites it cannot yet use.
- Policy: an organisation with no policy row shows a default at version 1 and the first PUT creates the row; the 428 for a missing `If-Match` also applies to the first PUT.
- Billing's status words I do not know (`canceled`, `expired`, `trialing`, `unpaid`) are mapped to `ended`, `ended`, `active`, `past_due`; anything else is logged and skipped.
- `subscription.changed` is subscribed only when `SEAT_SOURCE=billing`; the topic must exist (billing creates it).
- `seats.refresh_all` skips orgs that are on a pilot with staff seats.
- A request that waits more than `DB_POOL_TIMEOUT_SECONDS` for a connection answers 503, not 500.
- The metric `org_consumer_lag` is sampled every 10 s.

## Not done

- Real billing was not available: the usage report against it, the billing org-plan check and the real "route missing" warning are untested (the logic is covered with fakes and a stub).
- The absolute internal-latency target, as above.
- Everything in other repos (PHASES.md list): api-gateway routes, the public preview path and body cap; billing's subscription route and usage fixes; notification-service and its accept-link route; the frontend pages; conversation-service consuming `org.policy.changed` and `org.member.left`; auth's `EMAIL_VERIFICATION_REQUIRED=true` in production.
- No retry of a lost `subscription.changed` beyond the nightly sweep.

## Service status

**Production-ready (in this repo):** memberships and roles; the join transaction and seat rules (race-tested at 100 concurrent); email, CSV and link invites with derived tokens; the outbox and consumer with a DLQ and replay; jobs; domains, auto-join and join requests; policies; request size limits; one-log-line error reporting; metrics and alerts; a runbook and a launch checklist; 1,782 passing tests.

**Depends on other repos:** nothing reaches users until api-gateway routes it and the frontend has the pages; billing must run with `ORG_SERVICE_URL`/`ORG_SERVICE_TOKEN` set before the org plan is granted; `SEAT_SOURCE=billing` and the nightly refresh need billing's subscription route; usage reports need billing running (and are incomplete for people who left); invite emails go through our own SMTP until notification-service and its accept-link route exist; domain auto-join needs auth-service with email verification required.

**Top 5 risks:**
1. **Single-process throughput.** One process serves about 130 internal calls a second on this machine; 20 simultaneous callers see 150-600 ms. Run several replicas or workers before real traffic and check `max_connections` (lld.md section 10).
2. **The pool-starvation class of bug.** It was found only by the load script; any new code that opens a second transaction while the request session holds a connection brings it back. `test_pool_starvation.py` guards the paths that exist today.
3. **Nothing is wired from the outside yet** (gateway, billing's `ORG_SERVICE_URL`, frontend): the first real pilot depends on three other teams' changes.
4. **Email at scale:** one SMTP sender at `EMAIL_SENDS_PER_MINUTE` (60 by default) means a 2,000-student upload takes about 33 minutes to send, and the sender has never been used against a real SMTP provider.
5. **Untested against real billing and real DNS at scale:** the usage report, seat refresh and the org-plan grant were only verified with fakes; domain verification was checked live only against example.com.
