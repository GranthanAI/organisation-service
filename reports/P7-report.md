# P7: organisation-service, report

Part A fixed M1 and M2. Part B is Phase 7: email invites with derived (never stored) tokens, preview, accept, manage, staff resend, the send job, and the expire and purge jobs.

## Gap fixes

- **M1 done.** Org audit view: staff rows have `actor_id` null and `actor_label` "GranthanAI staff"; service and system rows null and "GranthanAI"; user rows keep `actor_id` and get their name or email (a deleted or unknown user shows "A former member"). Staff `reason`, `staff_*` and `service` detail keys are hidden in the org view. The staff route is unchanged.
- **M2 done.** `join()` refuses a user with no `known_users` row: 422 `UNKNOWN_USER`. A deleted user is still NotFound. Staff `POST members` therefore needs a known user. Tests got `ensure_known` and `new_known` helpers.

## Phase 7 as built

- **Token design (your version):** `invite_token = base64url(HMAC-SHA256(INVITE_TOKEN_SECRET, "invite:<id>:<token_version>"))`, only its SHA-256 is stored. `INVITE_TOKEN_SECRET` is 32+ bytes; production rejects the dev default or a short one. Phase 5 owner invites use the same scheme.
- **Migration `0007_invite_tokens`:** `token_version`, `resend_count`, `last_sent_at`, `purged_at` (plus CHECKs). No email CHECK existed to relax.
- **Event `org.invite.created`** is `{org_id, invite_id, email, role, org_name, inviter_name, expires_at}` with no token and no URL (schema `extra=forbid`). PHASES.md Contracts §3 and lld.md updated.
- **Email:** `email/sender.py` (console "DEV EMAIL", SMTP in a thread, 10 s timeout), `email/templates.py`, in-process job `invite.send {invite_id, token_version}`; `INVITE_EMAILS_VIA=local|notification`.
- **Routes:** create, preview (public), accept, list, resend, revoke, staff resend. Jobs `invites.expire` and `invites.purge` are daily and registered as recurring.

## Commits (range `5dbdc7a..HEAD`, no trailer, not pushed)

| Hash | Message |
| --- | --- |
| `889bfa4` | P7-fix-M1: org audit view hides who at GranthanAI acted |
| `5dbdc7a` | P7-fix-M2: join refuses a user with no known_users row |
| `882b3f4` | P7a: derived invite tokens, migration 0007, create invites |
| `8294d37` | P7b: email sender port and invite templates |
| `2073f06` | P7c-P7f: preview, accept, manage, staff resend, jobs, docs |

The commits for P7c to P7f are one commit because the service and route files are shared between them. This report is committed after these.

## Tests (one file at a time, final run at the end)

| File | Result |
| --- | --- |
| tests/unit/test_invite_tokens.py (new), test_tokens.py, test_config.py, test_event_schemas.py | all pass (unit dir: 580 passed) |
| tests/unit/test_invite_templates.py (new), test_email_sender.py (new) | 11 and 16 passed |
| tests/postgres/test_migrations.py | 0007 up, down, up, existing rows get defaults, nonsense values rejected |
| tests/api/test_invites_create.py (new) | 31 passed |
| tests/postgres/test_invite_send_job.py (new) | 13 passed (sent after commit, stale version skipped, retry with backoff, no link or address in logs) |
| tests/api/test_invite_preview.py (new) | 22 passed |
| tests/api/test_invite_accept.py (new) | 24 passed (two concurrent accepts give one membership) |
| tests/api/test_invites_manage.py (new) | 18 passed |
| tests/postgres/test_invite_jobs.py (new) | 8 passed (batches over 500) |
| every other file under tests/ | passed one file at a time; ruff clean; no `org_test_*` databases left |

## Exit check (live, dev stack, console sender, workers on)

1. Owner (dev JWT) invited a new address: 200 `created`; "DEV EMAIL" with the `/join/<token>` link appeared in the log.
2. `GET /org/v1/invites/preview` with no JWT: 200, `email_hint` `l***@example.com`.
3. Registered that address on the shared auth-service (8001) via its public API (201). **Login there needs email verification, so I could not get a real auth JWT**; I used a dev JWT with the same user id and email instead (auth-service was not edited). The user had reached `known_users` through the user event.
4. Accept: 200 `{org_name: "Seed College", role: "member"}`; `/org/v1/me` shows the org. `org.member.joined` with `source: "invite"` was read back from the topic.
5. Resend of a second invite: old link preview 404, new link 200; the resend email carried the new link. The `org.invite.created` topic message has no token or URL.
6. Searched every table in the dev DB for the three raw tokens: 0 hits.

Not checked by hand: the billing "member shows on the org plan" step (you said you run billing yourself).

## Decisions needed

- Resend conflicts and the limit follow the one code map: `INVITE_INVALID` is 404 and `RATE_LIMITED` is 429 with `Retry-After` (the spec text said 409).
- Resend is allowed for an invite past its expiry that is not yet accepted or revoked (it is revived with a fresh expiry).
- Kept `SMTP_USE_SSL` and `SMTP_STARTTLS` instead of adding `SMTP_USE_TLS`.
- A Phase 5 owner invite with a random hash gets its derived hash at the first send or resend. It also now emits `org.invite.created` (inviter shown as "GranthanAI").
- The daily limit counts invites created in the UTC day; a request that would cross it is refused whole (429).
- Duplicate addresses in one request are reported as `duplicate_in_request`.
- The console sender logs the link by design (dev only; production refuses it).

## Not done

- The internal route notification-service will need to get an accept link (listed in PHASES.md and lld.md, not built).
- Changes needed in other repos: api-gateway must treat exactly `GET /org/v1/invites/preview` as public; notification-service as above; frontend `/join/<token>` page.
