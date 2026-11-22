# Alerts

The alerts the owner should wire in Prometheus and Alertmanager (the service exposes `/metrics`). Every metric named here exists in
`app/telemetry/metrics.py`; `tests/unit/test_alert_doc.py` fails if one is renamed or removed, and if a "Runbook" section below is not
a heading in `docs/runbook.md`.

| # | Alert | Metric | Condition | For | Severity | Runbook section |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Outbox is not draining | `org_outbox_oldest_age_seconds` | `> 300` (the oldest unpublished event is older than 5 minutes) | 2 m | page | Outbox backlog |
| 2 | Outbox keys are stuck | `org_outbox_blocked_keys` | `> 0` | 15 m | page | Blocked outbox keys |
| 3 | Consumer is behind | `org_consumer_lag` | `> 1000` | 10 m | page | Consumer lag |
| 4 | Dead letters | `org_dlq_messages_total` | `increase(...[15m]) > 0` (or the DLQ topic is not empty) | 0 m | ticket | Replay the DLQ |
| 5 | Billing's hot-path lookup is slow | `org_internal_requests_seconds` | `histogram_quantile(0.99, sum by (le) (rate(org_internal_requests_seconds_bucket{route="/internal/memberships/{user_id}"}[5m]))) > 0.1` | 10 m | page | Slow internal lookups |
| 6 | The event loop is blocked | `org_event_loop_lag_seconds` | `> 0.5` | 5 m | page | Event loop lag |
| 7 | A background job gave up | `org_jobs_failed_total` | `increase(...[1h]) > 0` | 0 m | ticket | Failed jobs |
| 8 | Invite emails are being held back | `org_email_deferred_total` | `rate(...[5m]) > 0.5` (the send limit keeps deferring) | 30 m | ticket | Email is deferred |
| 9 | Usage reports fail | `org_billing_requests_seconds` | `sum(rate(org_billing_requests_seconds_count{result!="2xx"}[10m])) / sum(rate(org_billing_requests_seconds_count[10m])) > 0.2` | 10 m | ticket | Billing errors |
| 10 | Postgres is not ready | `org_dependency_up` | `org_dependency_up{dependency="postgres"} == 0` (also: `/ready` answers 503) | 2 m | page | Postgres is not ready |
| 11 | A worker is down | `org_worker_up` | `org_worker_up == 0` | 5 m | page | A worker is down |

Not alerts, but worth a dashboard: `org_joins_total{source,result}` (refusals by reason), `org_seat_full_total` (colleges hitting
their seat limit), `org_invites_created_total{kind}`, `org_email_sent_total`, `org_domain_checks_total{result}`.

Notes
- Metrics that are counters are exposed with the suffix `_total`; the gauges and histograms as written.
- The internal-latency threshold (100 ms) is for a production host; on a Windows dev machine the HTTP stack alone costs about 7 ms a
  request (see lld.md section 10).
- Redis and Kafka being down degrade the service (rate limits fail open, the outbox backs up) but do not stop it; they show up as
  alerts 1 to 3 and as `org_dependency_up{dependency="redis"|"kafka"} == 0`.
