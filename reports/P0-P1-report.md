# P0-P1: organisation-service, report

- **Commits** (range `4bf2d9f^..HEAD`; **pushed:** no, the owner pushes). The `P0a` to `P1h` hashes below; the report itself is committed after them.

  | Hash | Message |
  | --- | --- |
  | `128167d` | P0a: project setup (uv, deps, gitignore, gitleaks, pre-commit) |
  | `a2bda98` | P0b: config with production guard, structlog JSON logging, clock |
  | `ddebbfb` | P0c: app with /live, /ready, /metrics and correlation middleware |
  | `ca3fe3b` | P0d: Dockerfile, compose, Makefile, create_db script, .env.example |
  | `4bf2d9f` | P0e: README, lld, hld, PHASES |
  | `241c145` | P1a: enums and domain errors |
  | `f562a94` | P1b: permission rules |
  | `68854ee` | P1c: seat rules |
  | `7d790a1` | P1d: org lifecycle rules |
  | `e9dab4e` | P1e: membership rules |
  | `bcf71d7` | P1f: email and domain rules, public email domains |
  | `6814aee` | P1g: invite tokens |
  | `569ded4` | P1h: UUIDv7 ids, layering test, ruff config |

- **Contracts touched:** none implemented yet. Phase 0 added the `/live`, `/ready`, `/metrics` routes and every setting in the Settings table; Phase 1 added the rule tables the later contracts depend on (the Contracts section 6 plan and seat rules, the member-event enums). No Phase 2+ code (no models, migrations, repositories, other routes, Kafka producers or consumers).
- **Tests:** one file at a time, no full suite, no Testcontainers, no network in tests. All passed (table below).
- **Sibling repos:** all four were present and read only (`conversation-service`, `auth-service`, `api-gateway`; none edited). `_production_problems()` does **not** exist in api-gateway (see Decisions needed).

## Tests run (`.venv\Scripts\python -m pytest tests/<dir>/<file>.py -q`, one file at a time)

| File | Result | Covers |
| --- | --- | --- |
| tests/unit/test_config.py | 23 passed | each dev default rejected in production (JWT secret, issuer, internal tokens, billing token, DB password, DEBUG, console email, SMTP host, APP_BASE_URL), no secret values in the error |
| tests/api/test_health.py | 9 passed | `/live` 200; `/ready` 200, and 503 naming postgres, redis or kafka (fake probes, a raising probe); `/metrics`; correlation id echoed and generated |
| tests/unit/test_permission_rules.py | 110 passed | all 20 actions x 5 actors (owner, admin, member, staff support, staff admin), OwnRoleOnly, fail-closed |
| tests/unit/test_seat_rules.py | 27 passed | seats-1 / seats / seats+1, pilot 24/25/26 active, `None` seats (no cap), inactive states |
| tests/unit/test_org_lifecycle_rules.py | 37 passed | all 25 (from, to) pairs |
| tests/unit/test_membership_rules.py | 21 passed | last owner, role changes, transfer |
| tests/unit/test_email_rules.py | 73 passed | normalise, domain syntax, public domains |
| tests/unit/test_tokens.py | 6 passed | sha256, not reversible, differs per token, constant-time compare |
| tests/unit/test_ids.py | 8 passed | version/variant bits, time ordering, same-ms counter, overflow, clock stepping back, threads |
| tests/unit/test_layering.py | 29 passed, 1 skipped | `*_rules.py` pure; api and repositories rules (the skip is the empty `repositories/` parametrization, nothing exists there until Phase 2); checker self-tests |

`ruff check app tests scripts`: clean.

## Exit check

**Phase 0: DONE** for everything that can run here:
- `make setup` ran; `make db-create` ran twice (first "created", second "already exists", so idempotent); `make dev` booted on 127.0.0.1:8010.
- Live: `GET /live` 200; `GET /ready` 200 with shared infra up (`postgres`, `redis`, `kafka` all UP).
- Down case on the real stack: a second instance with `REDIS_URL` pointed at a dead port returned **503 `{"failed":["redis"]}`** with postgres and kafka UP. (It was started on 8011 and stopped; the shared Redis was not touched.)
- `GET /metrics` returns Prometheus text. `X-Correlation-ID` sent as `t-1` came back as `t-1`; with none sent, a UUID is generated.
- **Not verified:** `docker build` of the Dockerfile and `docker compose up` of this service's compose file were not run (no new containers, disk is limited). `make dev` and the tests were the check.

**Phase 1: DONE.** All eight Phase 1 test files exist and pass, including `test_layering.py`. `test_permission_rules.py` walks every (action, role) cell; `test_seat_rules.py` covers seats-1, seats, seats+1, pilot member 25 vs 26 and `seats=None`. The `*_rules.py` files import only the stdlib and `app/core`.

## Decisions needed (I took the option PHASES.md states, or the narrowest reading)

1. **`_production_problems()` in api-gateway.** PHASES.md says to copy it, but api-gateway has no such function: the guard is a pydantic `model_validator` (`_fail_fast_on_unsafe_production_config`) in `app/core/config.py`, as in conversation-service. I kept that shape and put the checks in a `Settings._production_problems()` method that the validator calls, so the name in PHASES.md exists. Confirm that is fine.
2. **Meaning of `OwnRoleOnly`.** PHASES.md does not define it. I read the HLD matrix ("Remove a member: Admin: Members only") as: allowed only when the target's role ranks below the actor's, so an admin removes members, never another admin or the owner. It fails closed if the target role is not given. Only `REMOVE_MEMBER` for `admin` uses it.
3. **Owner and `LEAVE`.** The HLD says "After transfer". The permission table says Allowed for owner; the "last owner cannot leave" rule is in `membership_rules.check_can_end`. Confirm that split.
4. **Staff actions.** The HLD matrix gives staff Write on org actions, `support` read-only. I mapped support to `VIEW_ORG`, `VIEW_USAGE`, `VIEW_AUDIT` only, and staff `admin` to every action. Staff reactivate is covered by `STAFF_SUSPEND`; staff close by `CLOSE_ORG`. The HLD names the staff role `ops_admin`; PHASES.md and auth-service use `admin`, which I followed.
5. **Rules files may not import each other.** The layering rule allows only the stdlib and `app/core`, so `membership_rules` cannot call `permission_rules`. Callers run `permission_rules.check(...)` first (stated in the docstring). Say so if you would rather allow rules-to-rules imports.
6. **`PILOT_MAX_MEMBERS` in a "pure" file.** `seat_rules.can_join` reads `settings.PILOT_MAX_MEMBERS` (from `app/core`, allowed) unless `pilot_max_members` is passed. Pilot cap is checked before seats, so a 25-seat pilot reports `PILOT_FULL`, not `SEATS_FULL`.
7. **Role vs StaffRole.** Both are `str` enums with an `admin` value, so `Role.ADMIN == StaffRole.ADMIN` is True. The permission tables are therefore separate and dispatched with `isinstance`. Worth remembering in Phase 3.
8. **Domains are ASCII only** (IDN must be sent as punycode, `xn--...`). Say so if colleges with non-ASCII domains matter.
9. **Console log renderer.** PHASES.md says structlog JSON; I use JSON in every environment (conversation-service uses coloured output in dev).
10. **Commit trailer.** Your memory notes say "no Co-Authored-By trailer", but this session's brief says to end commits with the trailer it gives, so every commit ends with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. If you want them stripped, say so (rewriting history is yours to run, nothing is pushed).
11. **PHASES.md location.** The folder layout puts `PHASES.md` at the repo root; it is in `docs/` (where you put it) and I left it there.

## Not done

- **Phase 2 and later:** by instruction (no models, migrations, repositories, other routes, Kafka producers or consumers). `/ready` uses simple connection probes only; the Kafka producer module holds only the probe.
- **`docs/hld.md`:** you had placed the HLD as `docs/GranthanAI org-service HLD.md`; I copied it to `docs/hld.md` (both are committed; delete the long-named one if you prefer). Nothing to paste.
- **`alembic.ini` and `app/db/migrations`:** Phase 2a. `make db-migrate` points at Alembic and fails until then (the Dockerfile copies `alembic.ini*` so it does not break the build).
- **`app/core/constants.py`, `plan_features.py`, `tests/support/` fakes (`FakeEmailSender`, `FakeDns`, `FakeBillingClient`), `scripts/seed_dev.py`, `scripts/smoke.py`:** not in the P0/P1 lists; later phases. `FixedClock` lives in `app/core/clock.py`; `tests/support/` will re-export it when it is created.
- **Docker image and this service's compose file:** written, not built or run (see Exit check).
- **Other repos:** nothing needed for Phases 0-1. The list in PHASES.md ("Changes needed in other repos") is unchanged; its first rows (billing `topics.txt` for the new topics) are needed by Phase 4.
- **Pre-commit / gitleaks hooks:** the config files are copied; I did not run `pre-commit install` (a per-clone step).
