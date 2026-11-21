# GranthanAI org-service HLD

Nov 8, 2026 · @ShubhChi

## Summary

Build org-service as a small, boring sibling of billing-service: same stack, same layer rules, its own `org` database, and the exact internal API billing-service already calls. It decides who belongs to which organisation, in what role, against how many seats; billing decides what that costs.

- **Stack.** Python 3.12, FastAPI, uv, PostgreSQL, Kafka with a transactional outbox, one image run as `org-api` and `org-worker`. No Valkey in v1: membership lookups are one indexed row, and billing already caches them for 60 s.
- **The contract is already written for you.** billing-service Stage F shipped an `OrgDirectory` client and an `org.member.*` consumer. org-service must answer those three endpoints and publish those two events first; that alone unblocks paid organisations.
- **One rule carries the money.** A join happens only inside a transaction that locks the organisation's seat row, counts active members and checks the seats billing sold. Two students accepting the last seat at once must not both get in.
- **Order.** Internal contract and events, then staff-created pilots, then email invites, CSV, links, domains, policies and usage reports. Phases are at the end, written so your Claude Code agent can take them one per session, like billing's `PHASES.md`.

Sources: the release plan (30 Oct 2026), the subscription plans workbook, and the `main` branch of all 11 GranthanAI repositories as pushed on 7 Nov 2026.

## What billing-service has already fixed

billing-service's code (`src/billing/adapters/orgs.py`, `adapters/kafka/schemas.py`, `api/rest/orgs.py`) is the spec for org-service's first phase. Where the repo's `contracts/org-service.md` disagrees with the code, build to the code and fix the markdown.

| Contract item | What billing expects | Note |
| --- | --- | --- |
| `GET /internal/memberships/{user_id}` | `{org_id, org_type: college\|company, role, status}`, 404 when none; only `active` grants the org plan | `contracts/org-service.md` says `/internal/users/{id}/membership`; the client calls `/internal/memberships/{id}` |
| `GET /internal/orgs/{org_id}` | `{name, type, gstin, billing_email, state_code, member_spend_cap_paise}` | `state_code` (2-digit GST code) is required before billing can invoice |
| `GET /internal/orgs/{org_id}/members` | `{user_ids: [...]}`, one response, no paging | Used for the pilot check (at most 25) and cache invalidation |
| Auth on those calls | `Authorization: Bearer <ORG_SERVICE_TOKEN>`, 2 s timeout, one retry | Billing caches answers 60 s and falls back to the personal plan if org-service is down |
| `org.member.joined`, `org.member.left` | JSON `{event_id, user_id, org_id, occurred_at, role, org_type}`, key `org_id` | Always send `org_type`: billing defaults a missing value to `company`, which gives a college student 60 messages a day instead of 30 |
| org-service calls billing | `GET /internal/orgs/{org_id}/usage?from=&to=[&format=csv]`, same bearer token | Per member and model group: messages and billable units |
| Staff money actions | Subscribe, seats, mark-paid, payment link, wallet top-ups live in `/admin/billing/v1` | admin-console calls billing for these, never org-service |

**Two gaps billing must close for org-service** (detail under Changes in other repos):

- org-service has no way to learn how many seats were sold. `subscription.changed` carries no `seats`, and no internal route returns an organisation's subscription.
- Org plan features (usage reports, shared projects, mode lock, shared-mode count) are not in `plans.yaml`, so org-service cannot gate them without copying the catalog.

## Scope

org-service owns membership and the admin's rules; every rupee stays in billing-service and every piece of content stays where it lives today.

| org-service owns | Lives elsewhere |
| --- | --- |
| Organisations: name, type (college or company), legal name, GSTIN, state code, billing email, status, pilot end | Prices, subscriptions, seats sold, invoices, wallets: billing-service |
| Memberships and roles: owner, admin, member; join, leave, remove, transfer ownership | Login, email verification, SSO: auth-service |
| Invites: single email, CSV for colleges, invite links, auto-join by verified email domain | Sending email: notification-service (a temporary adapter until it exists) |
| Seat enforcement against what billing sold; the 25-member pilot cap | Shared projects, shared and locked custom modes: conversation-service, asking org-service who is a member |
| Policies: locked modes on or off, approved mode ids, per-member spend cap | Shared-project documents: file-service, asking org-service |
| Audit log of every admin action | Message and model limits: billing-service's `CheckAndReserve` |
| Usage report for org admins (reads billing, adds names) | Members' conversations: never visible to org admins |

The release plan also lists "allowed model groups" as a policy. The workbook does not sell it on any org plan, so leave it out of v1.

## Architecture

One codebase, two processes, one database; the only synchronous callers are api-gateway and billing-service.

&#91;embedded content: org-service architecture · 2 processes, 1 database, Kafka in and out\]

`org-api` runs 2 replicas behind api-gateway; `org-worker` runs the outbox relay, the Kafka consumers and the jobs table with `SKIP LOCKED`, so a second replica is safe. The `org` database and role are org-service's alone; other services call its API or read its events.

## Data model

Nine tables plus billing's three plumbing tables, in one `org` database; ids are UUIDv7, times are UTC, emails are `citext` and lower-cased.

| Table | Key columns | Rule |
| --- | --- | --- |
| `organisations` | id, name, slug, type (`college`/`company`), legal\_name, gstin, state\_code, billing\_email, status (`pilot`/`active`/`pilot_expired`/`suspended`/`closed`), pilot\_ends\_at, created\_by | `state_code` must be set before billing invoices |
| `memberships` | org\_id, user\_id, role (`owner`/`admin`/`member`), status (`active`/`left`/`removed`), source (`staff`/`invite`/`csv`/`link`/`domain`), invited\_by, joined\_at, ended\_at | Partial unique index on `user_id WHERE status='active'` = one organisation per user; at least one active owner per org |
| `invites` | id, org\_id, kind (`email`/`csv`/`link`), email (null for links), role, token\_hash (SHA-256), max\_uses, uses, batch\_id, expires\_at, accepted\_at, accepted\_by, revoked\_at | Token shown once, never stored; email invites are bound to that email |
| `invite_batches` | id, org\_id, uploaded\_by, filename, rows\_total, rows\_ok, rows\_failed, status, report\_key | One row per CSV upload |
| `org_domains` | org\_id, domain, verify\_token, verified\_at, verified\_by, auto\_join | A domain is verified for one org only; public mail domains refused |
| `org_policies` | org\_id, lock\_modes, approved\_mode\_ids, member\_spend\_cap\_paise, version | `version` for `If-Match` updates |
| `seat_state` | org\_id, plan\_code, variant, seats\_bought, status, period\_end, features (jsonb), source\_event\_id, refreshed\_at | A copy of billing's truth; the row locked on every join |
| `known_users` | user\_id, email, verified\_at, deleted\_at | Built from auth-service's `user-events`; used to match invites and domains |
| `audit_log` | id, org\_id, actor\_id, actor\_kind (`user`/`staff`/`system`), action, target\_type, target\_id, details, correlation\_id, at | Insert-only: a trigger rejects `UPDATE` and `DELETE`, as billing does for usage |
| `outbox`, `processed_events`, `idempotency_keys` | as in billing-service | Exactly-once effects on at-least-once Kafka |

**Who counts as a seat.** Every active membership, owners and admins included, since that is what billing bills. Pending invites do not hold seats; acceptance fails when the org is full.

## Roles and permissions

Three org roles plus GranthanAI staff; every check lives in one pure `domain/permissions.py` table, so a test can walk every cell.

| Action | Owner | Admin | Member | Staff (`support` / `ops_admin`) |
| --- | --- | --- | --- | --- |
| See org name, own role, seats used | Yes | Yes | Yes | Read |
| Invite, resend, revoke invites; CSV; links | Yes | Yes | No | Write |
| Remove a member | Yes | Members only | No | Write |
| Make or remove an admin | Yes | No | No | Write |
| Transfer ownership, close the org | Yes | No | No | Write |
| Edit org details (GSTIN, state, billing email) | Yes | Yes | No | Write |
| Domains and auto-join | Yes | Yes | No | Write, incl. manual verify |
| Policies (mode lock, spend cap) | Yes | Yes | No | Write |
| Usage report, audit log | Yes | Yes | No | Read |
| Leave | After transfer | Yes | Yes | n/a |
| Create an org, set a pilot, suspend | No | No | No | `ops_admin` only |

The last active owner cannot leave, be removed or be demoted; the API answers `LAST_OWNER`.

## Key flows

Every way into an organisation ends in the same `join` use case, so the seat, pilot and one-org rules are written once.

### The join transaction (shared by every path)

1. Check the invite, link or domain is valid, and the user's email is verified in `known_users` (not yet there: 409 `EMAIL_NOT_VERIFIED`, the page retries).
2. `SELECT … FROM seat_state WHERE org_id = $1 FOR UPDATE`.
3. Refuse if the org is `suspended`, `closed` or `pilot_expired`; if the user already has an active membership (`ALREADY_IN_ORG`); or if active members ≥ seats (`SEATS_FULL`, or `PILOT_FULL` at 25).
4. Insert the membership, mark the invite used, write `audit_log` and the `org.member.joined` outbox row, commit.
5. The relay publishes; billing drops the member's cached entitlement and the org plan applies on their next message.

### 1. Staff create a college pilot

1. admin-console calls `POST /admin/org/v1/orgs` with name, type, owner email, `pilot_days: 30`. org-service creates the org in `pilot` with `seat_state` = 25 seats and an email invite to the owner.
2. admin-console then calls billing's override route for `org_free`; billing checks `/members` stays ≤ 25 every time.
3. Worker job `pilot.remind` sends `org.pilot.ending` at 7 days and 1 day; `pilot.expire` sets `pilot_expired`. Members stay members, billing's override lapses, and joins stop until a paid plan arrives.

### 2. Email invite and accept

1. Admin posts one or more emails. Each invite gets a 32-byte random token; only its SHA-256 is stored; expiry 14 days. Outbox: `org.invite.created` with the accept link.
2. The link opens `/join/<token>` in the frontend. `GET /org/v1/invites/preview?token=` (no login) shows the org name and inviter.
3. A new user signs up and verifies email with the token kept in the page; a signed-in user goes straight on. Then `POST /org/v1/invites/accept {token}`.
4. The invite email must equal the user's verified email (`INVITE_EMAIL_MISMATCH` otherwise), then the join transaction runs.

### 3. CSV upload for colleges

1. `POST /org/v1/orgs/{id}/invites/csv`: columns `email`, optional `name`, `role` (default member); at most 5,000 rows and 1 MB.
2. Validated at once: bad emails, duplicates, people already in another org, and rows beyond free seats are reported before anything is sent.
3. A worker job creates invites in chunks of 200 and emits one `org.invite.created` each; `GET /invite-batches/{id}` returns progress and a downloadable error report.

### 4. Invite link

Admin creates a link with `max_uses` and an expiry of at most 30 days; role is always member. Accepting uses the join transaction, with an optional rule that the email's domain must be one of the org's verified domains. Revoke at any time.

### 5. Domain auto-join

1. Admin adds `amity.edu`. Refused if it is a public mail domain (gmail.com, yahoo.com, outlook.com, rediffmail.com and a maintained list) or verified by another org.
2. Verify by DNS TXT `granthan-verify=<token>` (worker checks hourly), or by staff manually for colleges whose IT is slow, with a reason in the audit log.
3. On an `EmailVerified` event for that domain, a user with no org joins as member if seats allow; otherwise a join request waits for an admin. Many Indian students sign up with Gmail, so CSV stays the main path for colleges.

### 6. Remove, leave, delete

- Remove or leave sets `status`, `ended_at`, audit row and `org.member.left`; billing flips the person back to their own plan. Their personal conversations stay theirs.
- On `user.deleted`, every membership ends with `org.member.left`, pending invites to that email are revoked, and `known_users.deleted_at` is set. Audit rows keep the id, not the email.

### 7. Seats from billing

1. Consume `subscription.changed`; for `owner_type = org`, treat it as a hint and fetch `GET /internal/orgs/{id}/subscription` from billing (the new route), as billing does with Razorpay webhooks.
2. Upsert `seat_state` with plan, variant, seats, status, period end and features. A nightly job refreshes every org in case an event was lost.
3. When seats drop below active members (a seat decrease at renewal), nobody is removed: joins stop and the admin sees "over by N".

### 8. Policies

`PUT /org/v1/orgs/{id}/policies` with `If-Match: <version>` writes the row, the audit entry and `org.policy.changed` (full policy in the event). conversation-service consumes it for mode locking; billing keeps reading `member_spend_cap_paise` from `/internal/orgs/{id}` within its 60 s cache.

### 9. Usage report

`GET /org/v1/orgs/{id}/usage?from=&to=&format=json|csv` checks the plan includes usage reports (Org Pro and up), calls billing's usage route, then adds each member's name and email. CSV cells starting with `=`, `+`, `-` or `@` are prefixed with `'` so spreadsheets do not run them.

## APIs

Three route groups with three trust rules, copied from billing: public routes trust `X-User-Id` only with a valid `X-Gateway-Secret`; internal routes need a per-caller bearer token; admin routes also need `X-Staff-Role`. Errors are `application/problem+json` with a `code`; every `POST` takes an `Idempotency-Key`.

**Public, via api-gateway (`/org/v1`)**

| Route | Who | Does |
| --- | --- | --- |
| `GET /org/v1/me` | Any user | My org, role, plan, seats used / bought, features; 404 if none |
| `GET`, `PATCH /org/v1/orgs/{id}` | Member reads; owner, admin write | Org details |
| `GET /org/v1/orgs/{id}/members?cursor=&q=&role=` | Owner, admin | Paged member list with name and email |
| `PATCH`, `DELETE /org/v1/orgs/{id}/members/{user_id}` | Per the matrix | Change role, remove |
| `POST /org/v1/orgs/{id}/leave` | Member, admin | Leave |
| `POST /org/v1/orgs/{id}/transfer-ownership` | Owner | Hand over to an admin |
| `POST`, `GET /org/v1/orgs/{id}/invites`; `DELETE …/invites/{invite_id}`; `POST …/{invite_id}/resend` | Owner, admin | Email invites |
| `POST /org/v1/orgs/{id}/invites/csv`; `GET /org/v1/invite-batches/{batch_id}` | Owner, admin | CSV upload and progress |
| `POST`, `DELETE /org/v1/orgs/{id}/invite-links` | Owner, admin | Links |
| `GET /org/v1/invites/preview?token=` | Anyone (no JWT) | Org name, inviter, expiry |
| `POST /org/v1/invites/accept` | Signed-in user | Join |
| `POST`, `GET`, `DELETE /org/v1/orgs/{id}/domains`; `POST …/domains/{domain}/verify` | Owner, admin | Domains |
| `GET`, `PUT /org/v1/orgs/{id}/policies` | Owner, admin | Policies with `If-Match` |
| `GET /org/v1/orgs/{id}/usage` | Owner, admin | Usage report, JSON or CSV |
| `GET /org/v1/orgs/{id}/audit?cursor=` | Owner, admin | Audit log |

**Internal (bearer token per caller: billing, conversation, file)**

- `GET /internal/memberships/{user_id}`, `GET /internal/orgs/{org_id}`, `GET /internal/orgs/{org_id}/members`: exactly the shapes in the billing table above.

**Admin (`/admin/org/v1`, staff only)**

- `POST /orgs` (create, optional pilot), `GET /orgs?q=`, `POST /orgs/{id}/members` (add directly), `POST /orgs/{id}/suspend`, `/close`, `/extend-pilot`, `POST /orgs/{id}/domains/{domain}/manual-verify`. Every write needs a `reason`, stored in the audit log.

**Error codes**

| Code | HTTP | When |
| --- | --- | --- |
| `SEATS_FULL`, `PILOT_FULL` | 409 | No free seat |
| `ALREADY_IN_ORG` | 409 | User already has an active membership |
| `LAST_OWNER` | 409 | Would leave the org with no owner |
| `INVITE_INVALID`, `INVITE_EXPIRED` | 404, 410 | Bad, used, revoked or expired token |
| `INVITE_EMAIL_MISMATCH`, `EMAIL_NOT_VERIFIED` | 403 | Wrong account, or email not yet verified |
| `ORG_INACTIVE` | 409 | Suspended, closed or pilot expired |
| `FEATURE_NOT_IN_PLAN` | 403 | e.g. usage report on Org Free |
| `FORBIDDEN_ROLE` | 403 | Role not allowed for this action |
| `VERSION_CONFLICT` | 412 | Stale `If-Match` on policies |

## Security and privacy

The risks are seat leaks (lost money), invite abuse (your sending domain blacklisted) and colleges' student data.

| Risk | Control |
| --- | --- |
| Two accepts take the last seat | `FOR UPDATE` on `seat_state` in the join transaction; a load test with 100 concurrent accepts on 5 free seats must admit exactly 5 |
| Forged identity headers | `X-Gateway-Secret` middleware that fails closed, as billing's `GatewayAuthMiddleware`; gateway strips client `X-User-Id` and `X-Staff-Role` |
| api-gateway skips auth by mistake | Its `PUBLIC_PATHS` check is a substring match (`p in path`), so any path containing `/info`, `/live`, `/ready` or `/docs` skips the JWT. Fix to exact or prefix match before adding `/org/v1` routes |
| Stolen or guessed invite token | 256-bit tokens, SHA-256 at rest, single use for email invites, 14-day expiry, email-bound; links capped by `max_uses` |
| Invite spam through your domain | Per org per day: 200 single invites, 5,000 CSV rows; per inviter rate limit; staff can suspend |
| Domain hijack | DNS TXT or staff verification; public mail domains blocked; one org per domain |
| Admin overreach | Admins see names, emails and usage counts, never conversations, notes or documents |
| Silent changes | Insert-only `audit_log` with actor, reason and correlation id |
| Student data | Collect only email, name and role; purge unaccepted invite emails after 90 days; delete on `user.deleted`. India's DPDP Act 2023 applies to this data; have a lawyer confirm the college agreement and consent wording before the first college pilot |

## Changes in other repos

Four small changes block org-service's v1; the rest follow with shared projects in Phase 3 of the release plan.

| Repo | Change | Blocks |
| --- | --- | --- |
| billing-service | Add `seats` and `variant` to `subscription.changed`; add `GET /internal/orgs/{org_id}/subscription` → `{plan_code, variant, seats, status, period_end, features}` | Seat enforcement |
| billing-service | Org feature flags in `plans.yaml` org blocks: `usage_reports`, `shared_projects`, `mode_lock`, `shared_modes_max` (3 / 20 / 50) | Feature gating |
| billing-service | Fix `contracts/org-service.md` to the path the client calls | Docs only |
| auth-service | Staff claim in the JWT (`staff_role`: `support`, `billing_admin`, `ops_admin`); publish `user.deleted`; topics are `user-events` with an `event_type` field while billing expects a `user.deleted` topic, so pick one convention | Admin routes, deletion |
| api-gateway | Route `/org/v1/*`, `/admin/org/v1/*` with `X-Gateway-Secret`; strip and set `X-User-Id`, `X-Staff-Role`; public `GET /org/v1/invites/preview`; fix the `PUBLIC_PATHS` substring match | Any public use |
| notification-service | Templates for `org.invite.created`, `org.member.joined` (welcome), `org.pilot.ending`, seats almost full. Until it exists, org-service sends through an `EmailSender` port with an SMTP adapter, the pattern auth-service already uses | Invites |
| granthan-frontend | `/join/<token>` page that survives signup and verification; org admin pages (members, invites, CSV with error report, links, domains, policies, usage, audit); org badge in settings | Pilots |
| admin-console | Create org + pilot (calls org-service, then billing's override), find org, manual domain verify, seats via billing | Pilots |
| conversation-service | Consume `org.policy.changed` and `org.member.left`; shared projects, shared and locked modes asking `/internal/memberships` | Phase 3 |
| file-service | Shared-project document access through org membership; retrieval already asks file-service | Phase 3 |

## Build phases

Ten phases, one per agent session, each stopping at its exit check. Phase 3 is the one that makes billing's Stage F live; everything after it is product.

Ground rules, as in billing's `AGENTS.md`: domain code is pure; use cases take ports; import-linter, ruff and mypy `--strict` pass every phase; time comes from an injected `Clock`; real Postgres and Kafka through Testcontainers; no live email in tests.

| # | Phase | Builds | Exit check |
| --- | --- | --- | --- |
| 0 | Skeleton | uv project, layers (`domain`, `app`, `adapters`, `api`, `jobs`), settings, Dockerfile, Makefile, Alembic, `/healthz` `/readyz` `/metrics`, gateway-secret and service-token middleware | `make lint test` green; container starts; a request without the secret gets 401 and writes nothing |
| 1 | Domain | Org, Membership and Invite rules; permission matrix; seat and pilot rules; domain matching with the public-domain list | Property tests: no sequence of operations leaves an org with no owner or a user in two orgs |
| 2 | Schema | All tables, partial unique index, audit trigger, repositories | Migration up, down, up on a scratch database; `UPDATE audit_log` fails |
| 3 | Billing contract | The three internal routes; outbox relay; `org.member.joined` / `left` with `org_type` | billing's `HttpOrgDirectory` and `billing-org` consumer, pointed at a real org-service, pass billing's `test_orgs.py` exit scenario |
| 4 | Staff orgs and seats | Admin create, pilot, add member, suspend, close; `seat_state` from `subscription.changed` + billing fetch; nightly refresh | A pilot org takes 25 members and refuses the 26th; a paid org of 50 refuses the 51st |
| 5 | Email invites | `known_users` from `user-events`; invite, preview, accept, resend, revoke; leave, remove, roles, ownership transfer; `EmailSender` port | Invite → signup → verify → accept → billing grants the org plan, end to end in staging |
| 6 | CSV and links | Upload validation, `invite_batches`, chunked worker job, error report; invite links | A 2,000-row CSV with 50 bad rows sends 1,950 invites and reports the 50 |
| 7 | Domains | Add, DNS verify job, manual verify, auto-join on `EmailVerified`, join requests | A verified domain's new user joins without an invite; gmail.com is refused |
| 8 | Policies, usage, audit | Policies with `If-Match` and `org.policy.changed`; usage report JSON and CSV; audit API; pilot reminder and expiry jobs | An Org Free admin gets `FEATURE_NOT_IN_PLAN` on usage; an Org Pro admin's CSV matches billing's numbers |
| 9 | Hardening | Locust seat race, metrics, alerts, runbook | 100 concurrent accepts on 5 free seats admit exactly 5; p99 of `/internal/memberships` under 20 ms |

Later, only on demand: more than one org per user (drop the partial index, add `active_org_id`), SSO through auth-service, SCIM, self-serve company signup, groups inside a college (class sections).

## Open decisions

Each has a recommended answer that the design above already assumes; change any before Phase 1.

- [ ] **One organisation per user in v1.** Recommended yes (the release plan assumes it); the partial unique index makes it easy to lift later.
- [ ] **Seat overage.** Recommended: hard block at seats sold, and the admin asks for more. The alternative is to allow joins and bill a quarterly true-up, which billing already supports.
- [ ] **Owners and admins use a seat.** Recommended yes, so seats used equals what billing bills.
- [ ] **Org admins see member names and emails in usage reports.** Recommended yes; never content.
- [ ] **Who creates organisations in v1.** Recommended: staff only through admin-console, matching founder-led sales; self-serve company signup later.
- [ ] **Domain verification for colleges.** Recommended: DNS TXT or staff manual verification with a reason.
- [ ] **Event conventions.** billing uses dotted topics and a JSON envelope; auth uses one `user-events` topic with `event_type`. Pick one platform convention before org-service adds more topics.
- [ ] **Where org-shared content goes when a member leaves** (Phase 3, shared projects). Recommended: it stays with the organisation.
