# P2: organisation-service, report

Part A fixed the review gaps G1-G8 in Phase 0/1 code. Part B is Phase 2 (schema, models, repositories). Nothing past Phase 2 was written (no Phase 3+ code, no JWT dependencies, no Kafka producers or consumers, no outbox worker). **Pushed:** no.

## Gap fixes G1-G8

- **G1 done.** Dev `JWT_SECRET_KEY` is auth-service's shared dev secret in `config.py` and `.env.example`; the production guard rejects it (test proves it, and that the value is not echoed).
- **G2 done.** `/ready` is 503 only when Postgres is down; Redis or Kafka down gives 200 `DEGRADED`. Body `{status, required, failed, details}`. Every probe, Postgres included, runs under `asyncio.wait_for(READY_PROBE_TIMEOUT_SECONDS)`. `PHASES.md`, `lld.md` and README updated.
- **G3 done.** `seat_rules` no longer imports `app.core.config`; `pilot_max_members` is a required keyword. The layering test now rejects `app.core.config` in any `*_rules.py` (other `app.core` modules still allowed).
- **G4 done.** `pilot_expired -> pilot` and `suspended -> pilot` added, plus `reactivation_target(was_pilot, pilot_ends_at, now)`. I also added `suspended -> pilot_expired` (see Decisions needed, 1). Tests walk all 25 pairs. `lld.md` and `PHASES.md` P1d updated.
- **G5 done.** `EndReason` enum; `check_can_end(role, owner_count, reason)` blocks only `left` and `removed`; `user_deleted` and `org_closed` pass and return `EndCheck(owner_missing=True)` when the last owner goes. All four reasons tested. `memberships.end_reason` has a CHECK with these values.
- **G6 done.** Six actions added with the matrix you gave; `CLOSE_ORG` kept for the owner. Test walks every cell (142 cases); the matrix is in `lld.md`.
- **G7 done.** Nine errors with their P3c codes plus `join_decision_error(JoinDecision)`. `JoinDecision` moved to `app/core/enums.py` (re-exported through `seat_rules`) so `app/core` does not import `app/services`.
- **G8 done.** `docs/GranthanAI org-service HLD.md` deleted (`docs/hld.md` is the copy); README status fixed. `docker build -t organisation-service:dev .` **succeeded** (307 MB image, built in 16 s, 72 GB free). I did not start a container from it.

## Commits (range `c560db8^..d0e5e37`, 15 commits, no trailer)

| Hash | Message |
| --- | --- |
| `c560db8` | P2-fix-G1: shared dev JWT secret, refused in production |
| `aca333d` | P2-fix-G2: /ready 503 only for Postgres, DEGRADED for Redis/Kafka, probe timeouts |
| `064c157` | P2-fix-G3: seat rules take pilot_max_members as a required argument; rules may not import app.core.config |
| `dfb221a` | P2-fix-G4: lifecycle: expired/suspended pilots can return to pilot; reactivation_target |
| `774291d` | P2-fix-G5: EndReason enum; last-owner block only for left/removed, owner_missing flag for system endings |
| `cd08428` | P2-fix-G6: six more permission actions; matrix in lld |
| `c0a5b83` | P2-fix-G7: domain errors for the P3c codes and join_decision_error |
| `cfab148` | P2-fix-G8: remove duplicate HLD file, fix README status (docker build ok, 307MB) |
| `24f3ec6` | P2a: Alembic setup (async env.py, base, throwaway-database test support) |
| `dbf8b9f` | P2b: migration 0001_core |
| `a93b73b` | P2c: migration 0002_invites |
| `07ac0b7` | P2d: migrations 0003_domains_policies and 0004_grants |
| `e075ad0` | P2e: models and repositories |
| `b943bf7` | P2f: db error mapping, unit_of_work, and their Postgres tests |
| `d0e5e37` | P2f: lld matches the migrated schema; README db-migrate; import order |

## Tests run (`.venv\Scripts\python -m pytest <file> -q`, one file at a time, final run after the last code change)

| File | Result | Covers |
| --- | --- | --- |
| tests/postgres/test_migrations.py (new) | 6 passed | upgrade, downgrade to base, upgrade again; one step at a time; named and partial indexes; composite PKs; models match the migrated columns and nullability |
| tests/postgres/test_audit_guard.py (new) | 8 passed | raw and ORM `UPDATE`/`DELETE` fail with `restrict_violation`, rows unchanged; repository has only `add` and `list_page`; keyset paging |
| tests/postgres/test_one_active_org.py (new) | 7 passed | second active membership raises `AlreadyInOrg` (same or other org); ended plus new active is fine |
| tests/postgres/test_db_errors.py (new) | 8 passed | the three constraints map to `AlreadyInOrg`, `InviteAlreadyOpen`, `DomainTaken`; other unique and CHECK violations re-raised unchanged |
| tests/postgres/test_unit_of_work.py (new) | 7 passed | an exception rolls back membership, outbox and audit rows written in the block; original exception not wrapped; repositories never commit |
| tests/postgres/test_seat_lock.py (new) | 4 passed | second connection blocks while the first holds the lock, proceeds after commit; `lock_timeout` 300 ms expires; other orgs unaffected |
| tests/postgres/test_repositories.py (new) | 12 passed | a round trip per repository, keyset pages stable while members join, search via `known_users`, versioned policy update, job dedupe |
| tests/unit/test_exceptions.py (new) | 16 passed | error codes, `join_decision_error` mapping |
| tests/unit/test_config.py | 24 passed | touched (G1) |
| tests/api/test_health.py | 12 passed | touched (G2) |
| tests/unit/test_seat_rules.py | 28 passed | touched (G3) |
| tests/unit/test_org_lifecycle_rules.py | 45 passed | touched (G4) |
| tests/unit/test_membership_rules.py | 35 passed | touched (G5) |
| tests/unit/test_permission_rules.py | 142 passed | touched (G6) |
| tests/unit/test_layering.py | 43 passed | touched (G3); the `repositories/` and `api/` cases now run (no skip) |
| tests/unit/test_email_rules.py, test_tokens.py, test_ids.py | 73, 6, 8 passed | re-run, unchanged |

`uv run ruff check app tests scripts`: clean. Not run: the full suite (yours), `tests/integration`, `tests/load`.

## Exit check

- **`make db-migrate` on `organisation_service_db`: DONE.** Ran 0001 to 0004; `alembic_version` is `0004_grants`.
- **Downgrade to base and upgrade again: DONE** on throwaway databases (`test_migrations.py`), and step by step.
- **`UPDATE` and `DELETE` on `audit_log` fail: DONE** (`restrict_violation`, 23001).
- **Second active membership raises `AlreadyInOrg`: DONE.**
- **Seat lock blocks: DONE.**
- **No `org_test_*` databases left: DONE.** Checked with `SELECT ... LIKE 'org\_test\_%'`: 0 rows. One was left behind earlier when I stopped a hung test run by hand (`org_test_332d9d86`); I dropped it. The cause was my own test (cancelling an in-flight query), now replaced by a `lock_timeout` check. Each test file's database is dropped in a `finally`.
- **`lld.md` matches the tables, indexes and constraints created: DONE** (section 2 rewritten from the migrations; the models-versus-columns test guards drift).

## Decisions needed (I took the option PHASES.md or your brief states, or the narrowest reading)

1. **`suspended -> pilot_expired`.** G4 added `suspended -> pilot` only, but `reactivation_target` can return `pilot_expired` (pilot ended while suspended), and that would be an illegal move. I added `suspended -> pilot_expired` to the table. Remove it if you would rather staff first reactivate to `pilot` and let `pilot.expire` run.
2. **Expired-but-unrevoked invites hold the open-invite slot.** `ux_invites_open_email` is exactly as PHASES.md writes it (`accepted_at IS NULL AND revoked_at IS NULL`), so an invite past `expires_at` still blocks a new one for that email until the Phase 7 expiry job marks it. Decide in Phase 7 whether expiry sets `revoked_at` or the index gains a time condition (it cannot use `now()`).
3. **`seat_state.status` has no CHECK.** PHASES.md gives no values; billing's subscription statuses are not defined for this service. It is free text for now.
4. **`NotFound` code.** P3c has no `NOT_FOUND` code; I used `NOT_FOUND` (404) so Phase 3 has something to map. Confirm or rename.
5. **Extra constraints beyond PHASES.md** (each is small and in the migrations): `ck_memberships_ended` (active rows have no end fields, ended rows have both), `ck_org_domains_verified` (verified fields set together), domains lower-case, invite and policy numeric CHECKs, `errors` at most 5,000 rows, `ck_organisations_state_code` (two digits). Say if any should go.
6. **`lock_modes` is a boolean** (PHASES.md names the column but no type; the event field and P10 gating read as on/off).
7. **`audit_log.org_id` has no foreign key** (the log must outlive anything, and system or staff entries may have no org). `audit_log.target_id` is text, since targets are users, invites and domains.
8. **`unit_of_work()` lives with P2f, not P2e,** because it needs the error mapping; both are in the P2f commits. It also translates mapped `IntegrityError`s into domain errors, so a service sees `AlreadyInOrg` directly.
9. **`TRUNCATE` is not guarded** on `audit_log` (you asked for UPDATE and DELETE). The tests truncate between tests; the runtime role has no TRUNCATE right anyway.
10. **Repository extras beyond P2e's list:** `OutboxRepository.claim_due` and `JobRepository.claim_due`/`add_unless_open` are the queries Phase 4 will use; no worker was written. No repositories for `processed_events` or `idempotency_keys` (not in the P2e list); their models and tables exist.

## Not done

- **Phase 3 and later**, by instruction.
- **`org_app` grants not exercised.** The migration skips silently without the role (dev uses `postgres`), so I did not test the grant path against a real `org_app` role (creating roles in the shared Postgres was not asked for).
- **Docker image not started.** It builds; I did not run a container from it or `docker compose up` (no new containers).
- **Live check:** after the last change I started the API on 8010 once: `/ready` returned 200 with the new body, then I stopped it.
- **Other repos:** nothing new needed. The "Changes needed in other repos" table in PHASES.md is unchanged.
