# P3: organisation-service, report

Part A fixed the review gaps H1-H4 (one migration, `0005_plumbing_fixes`). Part B is Phase 3 (security and request plumbing). Nothing past Phase 3 was written: no business routes (the only new route is `GET /internal/ping`), no join or membership services, no Kafka producers or consumers, no workers. **Pushed:** no.

## Gap fixes H1-H4

- **H1 done.** `idempotency_keys.status_code` and `response` are nullable; new `state` (CHECK in_progress/done) and `locked_until`. Model and `lld.md` updated.
- **H2 done.** `ux_join_requests_pending` on `(org_id, user_id) WHERE status = 'pending'`; maps to the new `JoinRequestPending` (`JOIN_REQUEST_PENDING`, 409) in `app/db/errors.py`. A second pending request fails, another org or a decided request does not.
- **H3 done.** `ck_seat_state_status` (incomplete, active, past_due, suspended, cancelling, ended); `SeatStatus` enum added.
- **H4 done.** `invites.revoke_reason` with `ck_invites_revoke_reason` and `ck_invites_revoked_together`; existing revoked rows are backfilled with `revoked`. The expiry rule (revoke with reason `expired`, lazily before creating an invite and in the daily job) is in `lld.md`, and a test shows it frees `ux_invites_open_email`. Phase 7 implements it.
- **Migration 0005:** up, down and up works on throwaway databases (`test_migrations.py`; the downgrade drops idempotency reservations, which cannot exist in the old shape). `make db-migrate` took `organisation_service_db` to `0005_plumbing_fixes`. One test per new CHECK is in `test_db_errors.py`.

## Commits (range `a63aca6..1623d1b`, 11 commits, no trailer)

| Hash | Message |
| --- | --- |
| `fa6b535` | P3-fix-H1: migration 0005 (idempotency state, join-request, seat status, invite revoke_reason); idempotency model |
| `9334173` | P3-fix-H2: ux_join_requests_pending maps to JoinRequestPending |
| `3122057` | P3-fix-H3: SeatStatus and InviteRevokeReason enums |
| `185a5ca` | P3-fix-H4: invite revoke_reason model, CHECK tests, expiry rule in lld |
| `36161ae` | P3a: user identity (JWT verification, CurrentUser, require_staff, require_org_role, blacklist and deleted-user checks) |
| `5964f08` | P3b: service identity (INTERNAL_SERVICE_TOKENS, require_service, GET /internal/ping) |
| `10ef3de` | P3c: problem+json errors, one code-to-status map, new domain errors |
| `bb1901f` | P3d: idempotency (repository, service, IdempotentRoute) |
| `54edf10` | P3e: Redis fixed-window rate limits |
| `379d915` | P3f: audit service; lld and README for Phase 3 |
| `1623d1b` | P3a: loop the matrix inside three tests (same cells, a third of the time) |

The P3a test files use `register_exception_handlers` and `UpstreamUnavailable`, which arrive in the P3c commit, so the P3a commit alone does not pass its tests; the branch head does.

## Tests run (`.venv\Scripts\python -m pytest <file> -q`, one file at a time, final run)

| File | Result | Covers |
| --- | --- | --- |
| tests/unit/test_jwt.py (new) | 23 passed | valid; expired and the 10 s leeway; wrong secret; wrong issuer; each of sub, email, jti, exp, iat, iss missing; `alg: none` and HS512 refused; tampered payload; the error says nothing about why |
| tests/unit/test_staff_roles.py (new) | 18 passed | role combinations; `billing_admin` alone is no staff role; claim parsing; jti not in repr |
| tests/api/test_current_user.py (new) | 10 passed | blacklisted jti 401; Redis down: GET passes (warning logged, no jti or token in the log), POST/PUT/DELETE give 503; deleted user 401; unknown user passes (throwaway database + fakeredis) |
| tests/api/test_require_org_role.py (new) | 12 passed | all 20 actions for owner, admin and member match the matrix (403 for denied, OwnRoleOnly passes); non-member, ended member and member of another org get 404 for every action; staff do not pass; `require_staff` levels |
| tests/api/test_service_tokens.py (new) | 22 passed | parsing (empty name, duplicate name, shared token, no secrets in errors); every token compared with `compare_digest`; none, wrong and right token on `/internal/ping`; no secret in logs; no database access on refusal |
| tests/api/test_problem_json.py (new) | 40 passed | one case per error class and code; statuses match PHASES.md; every subclass is in the one map; validation shape (no input echoed); the 500 shape (no internals, stack trace in the log); 404 and 405 problem+json |
| tests/postgres/test_idempotency.py (new) | 25 passed | replay, canonical-JSON hash, mismatch, scope by actor and route template, concurrent second request 409 with `Retry-After`, expired lock taken over, 24 h expiry, 5xx and exception delete the reservation, 4xx stored, 429 and 401 not stored |
| tests/unit/test_rate_limit.py (new) | 21 passed | limit hit, `Retry-After`, window reset, separate users and buckets, TTL set, fail closed 503 vs fail open, buckets as specified, X-Forwarded-For trusted only from allowed peers |
| tests/postgres/test_audit_service.py (new) | 21 passed | user, staff, service and system kinds; correlation id stamped; forbidden keys at any depth; JSON-serialisable; 8 KB cap in bytes; rolls back with the caller's transaction; never commits |
| tests/postgres/test_migrations.py | 8 passed | touched (0005 up, down, up; backfill) |
| tests/postgres/test_db_errors.py | 25 passed | touched (H2 mapping, one test per new CHECK) |
| tests/unit/test_config.py | 26 passed | touched (issuer default, never empty) |
| tests/unit/test_layering.py, tests/api/test_health.py, tests/unit/test_exceptions.py | 47, 12, 16 passed | re-run |
| tests/postgres/test_audit_guard.py, test_one_active_org.py, test_unit_of_work.py, test_seat_lock.py, test_repositories.py | 8, 7, 7, 4, 12 passed | re-run after the shared fixtures moved to `tests/conftest.py` |

`uv run ruff check app tests scripts`: clean. Not run: the full suite (yours), `tests/integration`, `tests/load`.

## Exit check

- **H1-H4 and migration 0005: DONE** (up, down, up; `make db-migrate` reaches `0005_plumbing_fixes`).
- **All Phase 3 test files pass: DONE.** ruff clean.
- **No `org_test_*` databases left: DONE.** One was left behind during this session when my own `timeout 118` killed a slow test run before its cleanup could run (a hard kill skips the `finally`); I found it with the check below and dropped it. The test was then made a third as slow (same cells).
- **Live check: DONE.** Started the API on 127.0.0.1:8010 with the same uvicorn command `make dev` uses (not through `make`), then stopped it:
  - `GET /internal/ping` with no token: `401`, `application/problem+json`, `{"code":"UNAUTHENTICATED", ..., "correlation_id":"live-1"}` (the id I sent), `WWW-Authenticate: Bearer`;
  - with `Bearer dev-billing-token`: `200 {"caller":"billing"}`;
  - `GET /nope`: `404` problem+json with `code NOT_FOUND` and a generated `correlation_id`;
  - `/ready` still `200`; the log has 0 occurrences of the token or "Bearer" (the refusal logs only the path).
- **`lld.md` updated: DONE** (0005 changes and the invite expiry rule, the error-code table, the idempotency flow, the rate-limit buckets, identity and audit).

## Decisions needed (I took the option PHASES.md or your brief states, or the narrowest reading)

1. **Idempotency is a route class, not a plain dependency.** A dependency cannot see the response body, which has to be stored for replay. Public and admin routers use `APIRouter(route_class=IdempotentRoute)`; it reads the header (422 if missing or bad), reserves, replays and stores. It decodes the bearer itself to find the actor; if there is no valid token it runs the route untouched (the route's `get_current_user` then answers 401, nothing stored).
2. **What is not stored.** Besides 5xx and exceptions, I also delete the reservation on **401 and 429** (they depend on credentials or timing, so replaying them would lock a client out of its own retry). Other 4xx (403, 404, 409, 422) are stored as you specified. Say if 403/404 should be released too.
3. **`require_org_role` and OwnRoleOnly.** The dependency runs `permission_rules.check(action, role)`, but for an OwnRoleOnly cell (an admin removing someone) the target's role is unknown there, so it passes and the use case must call `check(action, role, target_role)`. Documented in the docstring and `lld.md`.
4. **Non-staff on a staff route gets 403** (`require_staff`), unlike non-members of an org, who get 404: the admin routes' existence is not a secret.
5. **`audit_service.record` takes extra keyword arguments** `org_id`, `staff` and `clock`. Your signature had no `org_id`, but `audit_log` has one; `staff=True` marks a user acting through an admin route. A service actor's name goes into `details.service` (there is no column for it).
6. **`JWT_ISSUER` can no longer be empty** (any environment), default `https://auth.granthan.com`. The old "JWT_ISSUER must be set" production check moved into the always-on validator. Added a `FORWARDED_ALLOW_IPS` setting (default `127.0.0.1`) for the per-IP buckets.
7. **Extra error classes.** Every code in the P3c table now has an exception class (`EmailNotVerified`, `InviteEmailMismatch`, `FeatureNotInPlan`, `VersionConflict`, `PublicEmailDomain`, `CsvInvalid`, `RateLimited`, `UpstreamUnavailable`, `ValidationFailed`, and the Phase 3 ones), so "one case per code" is testable. Plain HTTP errors get `METHOD_NOT_ALLOWED` or `HTTP_ERROR` codes that have no DomainError class.
8. **Shared service-token rule:** two services may not share a token (it would make the caller ambiguous); not in your brief.
9. **The Redis `blacklist:<jti>` key lives in db 0** of `REDIS_URL`, as in auth-service's default (`redis://localhost:6379/0`). If auth-service uses another database in an environment, set `REDIS_URL` to match.
10. **Test fixtures moved:** the throwaway-database fixtures are now in `tests/conftest.py` so `tests/api` can use them.

## Not done

- **Phase 4 and later**, by instruction. The idempotency cleanup job (rows older than 24 h) is a TODO in `lld.md`; until then old rows are ignored on read and taken over.
- **No route uses `IdempotentRoute`, `rate_limit`, `require_org_role` or `audit_service` yet**; they are built and tested on test-only apps and wired in from Phase 5 on.
- **billing's side:** its `ORG_SERVICE_TOKEN` must be set to the `billing:` token here (dev value `dev-billing-token`). That is a change in billing-service's environment, not done here.
- **`make dev` itself** was not used for the live check (same uvicorn command, run directly).
- **Other repos:** nothing new. api-gateway still needs the `/org/v1/*` route and the public `invites/preview` path later (see PHASES.md).
