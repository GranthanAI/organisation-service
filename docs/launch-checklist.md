# Launch checklist: before the first real college pilot

Tick every line. "Verify" says how. Anything that depends on another repo is marked (other repo): it is listed with its status in
`docs/PHASES.md` ("Changes needed in other repos").

## 1. Production settings (the guard passes)
- [ ] `ENVIRONMENT=production`, `DEBUG=false`. Verify: the service boots; `Settings._production_problems()` is empty (the service refuses to start otherwise, naming each problem, never the values).
- [ ] `JWT_SECRET_KEY` equals auth-service's, `JWT_ISSUER` equals auth-service's issuer. Verify: a real login JWT works on `GET /org/v1/me`.
- [ ] `INTERNAL_SERVICE_TOKENS` are 32+ character, unique tokens; billing's `ORG_SERVICE_TOKEN` equals the `billing:` entry. Verify: `curl /internal/ping -H "Authorization: Bearer <token>"` answers `{"caller":"billing"}`.
- [ ] `BILLING_BASE_URL` and `BILLING_SERVICE_TOKEN` point at the real billing. Verify: a usage report on a test org answers 200, not 503.
- [ ] `INVITE_TOKEN_SECRET` is a unique 32+ byte secret, stored in the secret manager (rotating it kills every open invite and link).
- [ ] `APP_BASE_URL` is the public frontend URL (invite links are built from it). Verify: a test invite email's link opens the join page.
- [ ] `DATABASE_URL` does not use the development password; the pool fits Postgres `max_connections` (replicas x workers x (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) + about 10; lld.md section 10).
- [ ] `FORWARDED_ALLOW_IPS` contains the gateway's address (the per-IP limit on the public invite preview trusts `X-Forwarded-For` only from it; if it is wrong, every visitor shares the gateway's IP and the limit locks real users out). Verify: two requests through the gateway from different client IPs count separately.
- [ ] `WEB_CONCURRENCY` (default 2) and the pool settings fit Postgres `max_connections`: `(DB_POOL_SIZE + DB_MAX_OVERFLOW) x WEB_CONCURRENCY x replicas <= max_connections - 20`.

## 2. Infrastructure
- [ ] Migrations applied: `alembic upgrade head` shows `0009_domains`. Verify: `alembic current`.
- [ ] Topics created: `make topics` (`org.member.joined`, `org.member.left`, `org.policy.changed`, `org.invite.created`, `org.pilot.ending`, `organisation.dlq`). (other repo) billing's `scripts/topics.txt` does not list the last four yet.
- [ ] Redis reachable (rate limits, token revocation, the email send counter). Verify: `GET /ready` shows `redis: UP`.
- [ ] Kafka reachable and `organisation-service` consumer group created. Verify: `GET /ready` shows `kafka: UP`; `org_consumer_lag` is 0 once idle.
- [ ] Workers: exactly the intended processes run with `WORKERS_ENABLED=true` (the outbox relay takes an advisory lock, so extra relays are harmless but wasteful). Verify: `org_worker_up{worker=...} == 1` for outbox, consumer and jobs.

## 3. The path from the outside
- [ ] (other repo) api-gateway routes `/org/v1/*` and `/admin/org/v1/*` to this service, forwards `Authorization`, makes `GET /org/v1/invites/preview` public, caps request bodies, and never routes `/internal/*`. Verify: through the gateway, preview works without a token and `/internal/ping` is a 404.
- [ ] (other repo) billing's `ORG_SERVICE_URL` and `ORG_SERVICE_TOKEN` are set (the token equals the `billing:` entry of `INTERNAL_SERVICE_TOKENS` here), so billing can read memberships and grant the org plan. Verify: after a test member joins, billing shows the org plan for them.
- [ ] (other repo) the frontend `/join/<token>` page and the org admin pages exist.

## 4. Email
- [ ] `EMAIL_SENDER=smtp` with `SMTP_HOST`, credentials, `EMAIL_FROM` on a domain with SPF and DKIM set up. Verify: invite a test address; the mail arrives, not in spam, and the link works.
- [ ] Run `uv run python -m scripts.send_test_email --to <your address>` ONCE against the production SMTP provider (production settings). It must print `OK` and the mail must arrive (check spam). Never skip this: the sender has only been tested with fakes.
- [ ] `EMAIL_SENDS_PER_MINUTE` is set to the SMTP provider's limit (default 60). A 2,000-student file takes about 33 minutes at 60 a minute; ask the provider for a higher limit before a big college.
- [ ] Decision: `INVITE_EMAILS_VIA=local` until notification-service exists (then `notification`, after it has the accept-link route).

## 5. Domains and auto-join
- [ ] Decision on `DOMAIN_AUTO_JOIN_ENABLED`. It is off by default. **Set (other repo) auth-service `EMAIL_VERIFICATION_REQUIRED=true` in production BEFORE turning it on**, because auto-join trusts the `EmailVerified` event. If on: `DNS_NAMESERVERS` set if the container's resolver is unreliable (for example `1.1.1.1,8.8.8.8`).
- [ ] Verify a domain end to end on a test org: add, publish the TXT record, `POST .../verify`, or the staff `manual-verify`.

## 6. Seats
- [ ] Decision: `SEAT_SOURCE=staff` (default; staff set seats after billing) or `billing`. **Set `SEAT_SOURCE=billing` only after billing's `GET /internal/orgs/{id}/subscription` is deployed** (verify: it answers for a subscribed test org); until then it keeps the existing seats and logs once.
- [ ] The first college: a pilot is created by staff (runbook: "Create a college pilot"); the owner got their invite or joined.

## 7. Backups and recovery
- [ ] Postgres is backed up (point-in-time if possible); a restore has been rehearsed on a copy. The `outbox`, `processed_events`, `jobs` and `audit_log` tables are part of the state.
- [ ] Kafka topic retention covers a weekend of downtime (the consumer resumes from its committed offset).

## 8. Monitoring
- [ ] Prometheus scrapes `/metrics`; the alerts in `docs/alerts.md` are wired to a pager or channel; each alert links the runbook section.
- [ ] Logs are collected; a request can be traced by `X-Correlation-ID` (every 4xx and 5xx is one `request failed` line).
- [ ] Someone on call has read `docs/runbook.md` and can: create a pilot, re-send invites, replay the DLQ, verify a domain by hand, fix an org with no owner.

## 9. Load
- [ ] Optional on staging: `python -m tests.load.seat_race` (exactly 5 of 100 admitted) and `python -m tests.load.internal_latency`; compare with the numbers in `reports/P10-report.md`.
