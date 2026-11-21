# P5: organisation-service, report

Part A fixed K1-K3. Part B is Phase 5 (seats, the join transaction, the billing contract, staff-run organisations). Nothing past Phase 5 was written: no `/org/v1` business routes (no me, members, invites, domains, policies), no invite sending, no CSV, no domains. **Pushed:** no.

## Gap fixes K1-K3

- **K1 done.** The outbox relay now blocks per key. When a row fails or is still backing off, every later row with the same key is skipped in that pass, and other keys keep going; created_at/id order and the single-relay advisory lock are unchanged. New gauge `org_outbox_blocked_keys`; a "stuck" warning (topic, key, attempts, last_error, never the payload) from the 10th attempt. Tests: a failure on key A does not stop key B; a later row of A waits for the earlier one; the gauge; the stuck log. `lld.md` updated.
- **K2 done.** `authenticate_user` now fills `known_users` from logged-in requests: creates a missing row, or updates a changed email, in its own short transaction (`sync_email`). It never touches a deleted user (they still get 401), never sets `email_verified_at` or `full_name`, caches "in sync" per `(user_id, email)` for 10 minutes in process memory (so a normal request does no write), and logs and swallows any failure. 10 tests (`tests/api/test_known_user_sync.py`).
- **K3 done.** `scripts/create_topics.py` (`make topics`, README line): the five produced topics plus `organisation.dlq`, 3 partitions and replication factor 1 by default (`--partitions`, `--replication-factor`), idempotent. 6 tests with a fake admin client. **Run once against the shared Kafka (127.0.0.1:9092):**
  ```
  found    org.member.joined
  found    org.member.left
  created  org.policy.changed
  created  org.invite.created
  created  org.pilot.ending
  found    organisation.dlq
  3 created, 3 already existed (127.0.0.1:9092)
  ```
  and a second run: `0 created, 6 already existed`. (`organisation.dlq` already existed; it was auto-created earlier.)

## Commits (range `ed3704d..7adf778`, 10 commits, no trailer)

| Hash | Message |
| --- | --- |
| `6a31572` | P5-fix-K1: outbox relay blocks per key, not globally; blocked-keys gauge and stuck-row warning |
| `bce7f69` | P5-fix-K2: fill known_users from logged-in requests (cached 10 min, own transaction, never fatal) |
| `aa2f62d` | P5-fix-K3: scripts/create_topics.py, make topics |
| `1dd9cd9` | P5a: seat service (staff-sourced seats, minimums per plan and variant, pilot to active, over_by audit); plan table in core |
| `9d9e24b` | P5b: join transaction (the only code that creates an active membership), with the seat race test |
| `31486cf` | P5c: leave and remove reuse membership_service.end from Phase 4 unchanged (change_role and transfer wait for Phase 6) |
| `2cf1988` | P5d: internal routes for billing (memberships with the K4 status rule, org, members), copied billing parser, timing histogram |
| `713efb3` | P5e: staff routes at /admin/org/v1 (create, members, seats, suspend, reactivate, extend-pilot, close, search), org service |
| `bafaf20` | P5f: pilot.remind, pilot.expire and org.close_members jobs; lld for Phase 5 |
| `7adf778` | P5f: make_staff_token dev script, README |

The report itself is committed after these. P5c is an empty commit on purpose: `membership_service.end` from Phase 4 is reused unchanged.

## Tests run (`.venv\Scripts\python -m pytest <file> -q`, one file at a time, final run at the end of the session)

| File | Result | Covers |
| --- | --- | --- |
| tests/postgres/test_seat_service.py (new) | 37 passed | plan and variant validation, minimums per plan and variant (below, at, above), payg no cap, pilot cap, a paid plan moves a pilot or expired pilot to active, over_by audited, `SEAT_SOURCE=billing` refused |
| tests/postgres/test_join_service.py (new) | 36 passed | each refusal (ORG_INACTIVE, SEATS_FULL, PILOT_FULL, ALREADY_IN_ORG, incomplete seats, deleted user); the unique index mapped through `unit_of_work` and through the caller's own flush; owner rules; invite uses and accepted fields; audit row; joined event payload and key; rollback leaves nothing |
| tests/postgres/test_seat_race.py (new) | 5 passed | 20 concurrent joins into 5 free seats admit exactly 5, 15 get SEATS_FULL, exactly 5 joined events; pilot cap and same-user races under concurrency; join vs leave |
| tests/api/test_internal_routes.py (new) | 26 passed | shapes field by field; 404s; K4 status (`active` for pilot/active, else `suspended`, `pilot_expired`, `closed`); alias route; members ordering and the 10,000 cap (exactly 10,000 ok, 10,001 gives 422 and a log); 401 without a token; billing's own copied parser reads every response; the timing histogram |
| tests/api/test_admin_orgs.py (new) | 78 passed | create pilot with a known owner (joins) and with an unknown owner (open invite, no token in the response or logs); slug clash and slug cleaning; GSTIN and state-code validation; the 26th pilot member gets PILOT_FULL; seats set; suspend, reactivate back to pilot and to pilot_expired; extend an expired pilot; close (status at once, batches of the close job, resume after a crash, left events); support can read but not write (every write route); missing, short or long reason gives 422; Idempotency-Key replay creates one org; search and cursor |
| tests/postgres/test_pilot_jobs.py (new) | 17 passed | remind at -7d and -1d (each once); expire; skip after conversion, suspension or close; stale reminder after an extension; extend reschedules; members stay members and billing stops granting |
| tests/api/test_known_user_sync.py (new) | 10 passed | K2 |
| tests/unit/test_create_topics.py (new) | 6 passed | K3 |
| tests/postgres/test_outbox_worker.py | 23 passed | touched (K1) |
| tests/api/test_problem_json.py | 41 passed | touched (new error code `SEATS_MANAGED_BY_BILLING`) |
| tests/api/test_current_user.py, test_health.py, test_require_org_role.py, test_service_tokens.py | 10, 12, 20, 22 passed | re-run |
| tests/postgres/test_idempotency.py | 30 passed | re-run after the route template change |
| tests/postgres/ (audit_guard, audit_service, consumer_worker, db_errors, job_runner, membership_service, migrations, one_active_org, repositories, seat_lock, unit_of_work, user_event_handlers) | 8, 21, 18, 25, 19, 20, 9, 7, 12, 4, 7, 19 passed | re-run |
| tests/integration/test_kafka_roundtrip.py | 3 passed | re-run (shared Kafka) |
| tests/unit/ (config, email_rules, event_schemas, exceptions, ids, jwt, layering, membership_rules, org_lifecycle_rules, permission_rules, rate_limit, seat_rules, staff_roles, tokens, worker_manager) | 26, 73, 29, 16, 8, 23, 54, 35, 45, 142, 21, 28, 18, 6, 8 passed | re-run |

`uv run ruff check app tests scripts`: clean. Not run: the full suite (yours), `tests/load`. `test_admin_orgs.py` takes about 2 minutes (78 tests, each with its own seeded organisation).

## Exit check

- **K1-K3 done with tests; K3 run once against the shared Kafka: DONE** (output above).
- **All Phase 5 test files pass; ruff clean; no `org_test_*` databases or topics left: DONE** (0 and 0 after the last file).
- **Live check, organisation-service only: DONE.** `WORKERS_ENABLED=true`, shared Kafka up, the API started with uvicorn on 127.0.0.1:8010 (the `make dev` command without `--reload`, so Ctrl+C reaches one process; a temporary launcher outside the repo, deleted afterwards). A staff JWT came from `scripts/make_staff_token.py` (signed with the dev secret and issuer; auth-service untouched).
  1. Seeded a known owner, then `POST /admin/org/v1/orgs` (college pilot): 201, `status pilot`, `owner_joined true`. Two members added with `POST /orgs/{id}/members`: 201 each.
  2. `GET /internal/memberships/{id}` with the billing token: **`active`** for a member and for the owner.
  3. `suspend`: 200 `suspended`; the same call now shows **`suspended`**. `reactivate`: 200 `pilot`; it shows **`active`** again.
  4. `GET /internal/orgs/{id}` returned the org (name, type, gstin `09AAAAA0000A1Z5`, state `09`, cap null); `/members` returned 3 ids; no token gave 401.
  5. **`org.member.joined` for all three users was read back from the topic**: keys are the user ids, roles `owner`, `member`, `member`, `org_type college`, `source staff`. `org_outbox_pending 0`, `org_outbox_blocked_keys 0`.
  6. Ctrl+C stopped the app in 0.9 s. (My script's count of `org_internal_requests_seconds` read 0 because its regex stopped at the `{user_id}` braces in the label; the histogram itself is covered by a test that reads its sum.)
- **Cross-service check with billing-service: NOT DONE, and why.** The shared dev billing is already running on 8090 (healthy), and `scripts/dev_up.sh` says that one instance serves every repo's tests and others must not start their own. Its `.env.dev` has **no** `ORG_SERVICE_URL` or `ORG_SERVICE_TOKEN` (so it runs with no org directory and refuses pilots). Making it call us means restarting that shared instance with those two settings, which would interrupt the other services' work and needs billing's env changed; I did not do either. **What you must do:** in billing-service's `.env.dev` set `ORG_SERVICE_URL=http://127.0.0.1:8010` and `ORG_SERVICE_TOKEN=dev-billing-token` (the `billing:` entry of our `INTERNAL_SERVICE_TOKENS`), run `scripts/dev_down.sh` then `scripts/dev_up.sh`, start organisation-service (`make dev`), create a pilot and add a member as in the live check, then look in billing's API log for `GET /internal/memberships/<id>` and for the consumed `org.member.joined`, and check the member's entitlement shows the org plan. What I did prove: billing's own copied parser reads every response (`test_internal_routes.py`), the events on the topic have billing's fields (`test_event_schemas.py`), and K4 makes suspended and expired orgs report a status other than `active`.
- **`lld.md` updated: DONE** (the join transaction, `seat_service` rules, the internal route shapes including the K4 status rule, the admin routes, the pilot and close jobs, K1 and K2).

## Decisions needed (I took the option PHASES.md or your brief states, or the narrowest reading)

1. **A non-pilot org starts with seats `incomplete`, and its FIRST owner may still join.** Your brief says "incomplete means no seats; can_join refuses with SEATS_FULL", but then the owner named at creation could never join an org whose seats are not set. I let the first owner (role OWNER, no active owner yet, seats incomplete or missing) bypass the seat check; every later member gets `SEATS_FULL` until staff set seats. Say if you would rather the owner stay pending (an invite) until seats exist.
2. **`SEAT_SOURCE=billing` gives 409 with code `SEATS_MANAGED_BY_BILLING`**, not `VALIDATION_ERROR` (that code is 422 everywhere; 409 and 422 cannot both be right). Message "seats come from billing".
3. **Reactivation reads `was_pilot` from the seat plan** (`seat_state.plan_code == "pilot"`), not from the suspend audit row; the audit still records `was_pilot` for the trail. A converted org has a paid plan, so it reactivates to `active`. Say if you want the audit row to be the source.
4. **`ALREADY_IN_ORG` is checked before the seat rules** (right after the lock), so a user already in the org who tries again gets `ALREADY_IN_ORG`, not `SEATS_FULL`. The unique index remains the final guard.
5. **`seat_state.status` `incomplete` and `ended` both mean no seats**; the other statuses (`active`, `past_due`, `cancelling`, `suspended`) use the seat count. Phase 10 should revisit `past_due` and `cancelling`.
6. **`close` also drops the open pilot jobs and revokes link invites too**, not only email and CSV ones. The close job ends members through `membership_service.end`, so each gets an `org.member.left` with reason `org_closed`.
7. **A route template is now rebuilt from the request** (`app/api/route_template.py`) for idempotency scoping and metric labels. This FastAPI version nests included routers lazily, so a route object only knows its relative path; the old approach would have scoped keys by a relative path.
8. **`extend-pilot` on a pilot_expired org** writes two audit rows (the status change, then the extension with the old and new end). A pilot whose seat plan is no longer `pilot` keeps its `period_end`.
9. **K1 edge:** the relay reads the oldest 100 unpublished rows per pass; if 100 rows of stuck keys sit at the head, newer keys wait until they clear. Not an issue at today's volumes; a keyset fix is easy if it ever matters.
10. **Dev billing was left untouched** (see the cross-service check). I also did not create or edit anything in billing-service.

## Not done

- **Cross-service check with billing** (above: what you must do).
- **Phase 6 and later**, by instruction: no `/org/v1` routes, no `change_role` or `transfer_ownership` (Phase 6 needs them), no invite sending, CSV or domains.
- **`make dev` itself** was not the process used for the live check (same app, started with uvicorn directly).
- **Other repos:** nothing edited. For the billing check you must set the two `ORG_SERVICE_*` values in its `.env.dev`.
