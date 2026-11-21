# P6: organisation-service, report

Part A fixed L1 (and added the owner-bypass test you asked for). Part B is Phase 6: the `/org/v1` API for organisation owners and admins (`me`, details, members, leave, transfer, audit) plus the staff reads of members and audit. Nothing past Phase 6 was written: no invite routes (create, preview, accept), no CSV, no invite links, no domains, no policies, no usage report. **Pushed:** no.

## Gap fix L1

- **L1 done.** `OutboxRepository.oldest_unpublished(limit, now)` now leaves out every row whose KEY has an unpublished row with `next_attempt_at > now` (a `NOT EXISTS` on the same table), so the batch of 100 is always filled with keys that can be published. The relay's per-key skip stays as a second guard. A key's rows come back as soon as its stuck row is due for a retry.
  - Test: 150 rows of 3 stuck keys at the head plus 10 rows of a healthy key: one pass publishes the 10 (the old query would have returned 100 stuck rows and published none). Also tested: the query itself with and without `now`, and a key coming back once its stuck row is due.
  - **EXPLAIN on a 10,000-row table (100 unpublished): no sequential scan.** The plan is a nested-loop anti join using the existing partial index `ix_outbox_unpublished` on both sides, so I added **no new index** (a test asserts `Seq Scan` is absent from the plan).
- **Owner cannot bypass a FULL seat_state: done** (two tests in `test_join_service.py`): a full seat_state or a full pilot refuses an OWNER with `SEATS_FULL` / `PILOT_FULL`; the bypass needs incomplete or missing seats AND no active owner.

## Commits (range `adb3af6..dd03cd1`, 5 commits, no trailer)

| Hash | Message |
| --- | --- |
| `02e5d24` | P6-fix-L1: outbox claim skips every key that has a row backing off, so stuck keys cannot starve the batch; owner-bypass test |
| `73f2e86` | P6a: GET /org/v1/me (org, role, seats, features); csv_invites, invite_links and domains on the plan table |
| `7f27b67` | P6b: organisation details (GET and PATCH /org/v1/orgs/{id}); GSTIN and state-code match; shared cursor helper |
| `13ef865` | P6c: members API (list, change role, remove, leave, transfer ownership), status gate, lock order, staff member list |
| `dd03cd1` | P6d: audit log API for the organisation and for staff; lld and PHASES for Phase 6 |

The report itself is committed after these.

## Tests run (`.venv\Scripts\python -m pytest <file> -q`, one file at a time, final run at the end of the session)

| File | Result | Covers |
| --- | --- | --- |
| tests/api/test_me.py (new) | 21 passed | no org gives 404; owner, admin and member views; pilot, payg (`bought` null), incomplete and ended, over_by; features per plan; no seat row; reads in every status; two queries; the `/org/v1/me` timing label |
| tests/api/test_org_details.py (new) | 42 passed | any member reads (no audit, no seat details); a member cannot patch (403); admin and owner can; GSTIN and state rules and the GSTIN/state match; an old mismatch does not block unrelated edits; empty body 422; audit lists only changed fields; no-change writes no audit; closed gives `ORG_INACTIVE`; a non-member gets 404 |
| tests/api/test_members.py (new) | 56 passed | **120 members page at 50 as 50/50/20, 120 unique ids**; paging stable while members join or leave between pages; limit cap, bad cursor, q by email and name, role filter, row shape; admin removes a member but not an admin or the owner, owner removes an admin; nobody removes themselves; role-change rules (no owner, not yourself, same role is a no-op, no Kafka event); `org.member.left` on removal; leave and the last owner; transfer happy path and every refusal; 404 for a non-member on every route; suspended and expired allow changes, closed refuses; Idempotency-Key replay of leave and transfer; **exactly one audit row per write, three for transfer** |
| tests/postgres/test_member_concurrency.py (new) | 14 passed | two admins removing the same member give one success and one 404; transfer and remove at once (5 rounds each, two scenarios) never deadlock and stay consistent; change_role and transfer; 12 members hit from two sides end exactly once; the lock order (seat_state before any membership) |
| tests/api/test_audit_api.py (new) | 14 passed | newest-first paging; filters by action and actor; staff reasons hidden and `staff_` details dropped for the org; the staff route shows everything; a member gets 403, a stranger 404 |
| tests/api/test_admin_members.py (new) | 7 passed | support and staff admin read; same shape and pagination; a non-staff caller (even the org owner) gets 403 |
| tests/postgres/test_outbox_worker.py | 27 passed | touched (L1: starvation, query, key comes back, EXPLAIN) |
| tests/postgres/test_join_service.py | 38 passed | touched (owner bypass) |
| tests/postgres/test_audit_service.py | 21 passed | touched (two new action names) |
| tests/api/ (admin_orgs, current_user, health, internal_routes, known_user_sync, problem_json, require_org_role, service_tokens) | 78, 10, 12, 26, 10, 41, 20, 22 passed | re-run |
| tests/postgres/ (audit_guard, consumer_worker, db_errors, idempotency, job_runner, membership_service, migrations, one_active_org, pilot_jobs, repositories, seat_lock, seat_race, seat_service, unit_of_work, user_event_handlers) | 8, 18, 25, 30, 19, 20, 9, 7, 17, 12, 4, 5, 37, 7, 19 passed | re-run |
| tests/integration/test_kafka_roundtrip.py | 3 passed | re-run (shared Kafka) |
| tests/unit/ (config, create_topics, email_rules, event_schemas, exceptions, ids, jwt, layering, membership_rules, org_lifecycle_rules, permission_rules, rate_limit, seat_rules, staff_roles, tokens, worker_manager) | 26, 6, 73, 29, 16, 8, 23, 58, 35, 45, 142, 21, 28, 18, 6, 8 passed | re-run |

`uv run ruff check app tests scripts`: clean. Not run: the full suite (yours), `tests/load`. `test_admin_orgs.py` (2 min) and `test_members.py` (1.5 min) are the slow ones.

## Exit check

- **L1 and the owner-bypass test: DONE.** All Phase 6 test files pass; ruff clean; **no `org_test_*` databases or topics left** (0 and 0 after the last file).
- **PHASES.md exit check: DONE.** A seeded org of 120 members pages correctly at 50 (3 pages of 50, 50 and 20; 120 unique ids), and every write in this phase left exactly one audit row (`test_every_write_leaves_exactly_one_audit_row_and_transfer_three`: role change 1, remove 1, details edit 1, leave 1, transfer 3 = `member.role_changed` x2 + `org.ownership_transferred`).
- **Live check: DONE.** `WORKERS_ENABLED=true`, shared Kafka up, the API started with uvicorn on 127.0.0.1:8010 (the `make dev` command without `--reload`; a temporary launcher outside the repo, deleted afterwards). User JWTs came from `scripts/make_staff_token.py` (no roles, one per person). On a seeded org of four (owner, admin, member, second):
  1. `GET /org/v1/me` as owner: 200, `role owner`, seats `{org_pro, active, used 4, bought 50, over_by 0}`, `lock_modes true`.
  2. `GET /orgs/{id}/members` as the admin: 200, the four people in join order.
  3. The admin removed the member: **204**; the admin removing themselves: 422; **`org.member.left` was read back from the topic** (`reason removed`, `role member`, `org_type college`).
  4. The owner transferred ownership to the admin: 200 `{owner_id: admin, previous_owner_id: owner, previous_owner_role: admin}`; `/me` now says owner for the admin and admin for the old owner.
  5. The audit log (as the new owner) showed `org.ownership_transferred`, two `member.role_changed`, `member.removed`, all `actor_kind user`.
  6. **The removed member's own `/org/v1/me` gives 404.**
  7. Ctrl+C stopped the app in 1.2 s.
- **`lld.md` updated: DONE** (every `/org/v1` route with its action, status gate and errors; the lock order; `change_role` and `transfer_ownership`; the no-event note for role changes).
- **`PHASES.md` "Changes needed in other repos": DONE** (api-gateway must route `/org/v1/*` and `/admin/org/v1/*` to organisation-service forwarding `Authorization`, and never route `/internal/*`).

## Decisions needed (I took the option PHASES.md or your brief states, or the narrowest reading)

1. **The caller's role is re-read under the lock.** A transfer can change the caller's role between the request's role lookup and the write, and `OrgContext.role` would be stale. `remove_member` and `change_role` now lock the caller's and the target's rows together (id order, after `seat_state`) and run `check_target` with a copy of the context carrying the fresh role. Result: an old owner who was just replaced cannot remove the new owner (403), tested. `change_role` and `transfer_ownership` take the `OrgContext` as their `actor` argument (your signature said `actor`), because they need the role and `check_target`.
2. **Status codes you did not give:** changing the role of an owner, and setting `owner`, are both 422 `VALIDATION_ERROR` ("use transfer-ownership"); changing your own role is 403 `FORBIDDEN_ROLE`; removing yourself is 422; the target of a transfer who is not an active admin, or who is not a member at all, is 422 (not 404, because the caller is a legitimate member).
3. **GSTIN/state match also applies on create.** Your brief put the rule in the shared validation function, so `POST /admin/org/v1/orgs` now refuses a GSTIN whose first two digits differ from `state_code` too. On PATCH, the existing value of the other field is used when only one is sent, but existing data is not re-checked when you edit something else (a test covers it).
4. **`/me` timing:** "add org_me_requests to the timing histogram labels" is done as `org_internal_requests_seconds{route="/org/v1/me"}` (the same route-template label as every other route), not a new metric name.
5. **Audit visibility.** For the organisation's view I hide `reason` unless `actor_kind` is `user`, and drop top-level `details` keys starting with `staff_`. Nothing is hidden about staff actions themselves (`actor_id` and `actor_kind` stay), so an org admin can see that staff changed seats. Say if staff `actor_id` should be hidden too.
6. **Status gate scope:** `transfer-ownership` is gated like the other writes (allowed in suspended and pilot_expired, refused when closed). `PATCH /orgs/{id}` also refuses in a closed org.
7. **`q` search** matches `known_users.email` and `full_name` only for people who are known users; a member without a `known_users` row shows `name` and `email` as null and cannot be found by search.
8. **No new migration and no new index** (EXPLAIN showed none was needed).

## Not done

- **Phase 7 and later**, by instruction: no invite create, preview or accept routes, no CSV, invite links, domains, policies or usage report. `features` already lists `csv_invites`, `invite_links` and `domains` as true for every plan, ahead of their routes.
- **The api-gateway change** (route `/org/v1/*` and `/admin/org/v1/*`, never `/internal/*`) is listed in `PHASES.md` and not made (other repo).
- **The billing cross-service check** from Phase 5 is yours to run, as you said.
- **`make dev` itself** was not the process used for the live check (same app, started with uvicorn directly).
