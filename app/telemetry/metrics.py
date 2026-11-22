"""Prometheus metrics (default registry, so process metrics come for free)."""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# --- outbox relay ---
OUTBOX_PENDING = Gauge("org_outbox_pending", "Outbox rows not yet published")
OUTBOX_OLDEST_AGE = Gauge("org_outbox_oldest_age_seconds", "Age of the oldest unpublished outbox row (0 when empty)")
OUTBOX_BLOCKED_KEYS = Gauge("org_outbox_blocked_keys", "Keys with a failed or backing-off outbox row in the last pass")
OUTBOX_PUBLISH_FAILURES = Counter("org_outbox_publish_failures", "Outbox publishes that failed (counter, _total)")

# --- internal routes (billing) ---
INTERNAL_REQUESTS = Histogram("org_internal_requests_seconds", "Duration of /internal requests", ["route"],
                              buckets=(0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0))

# --- jobs ---
JOBS_FAILED = Counter("org_jobs_failed", "Jobs given up on after their last attempt", ["kind"])

# --- email sending ---
EMAIL_SENT = Counter("org_email_sent", "Invite emails sent (counter, _total)")
EMAIL_DEFERRED = Counter("org_email_deferred", "Invite email sends deferred by the global send rate (counter, _total)")

# --- domain verification ---
DOMAIN_CHECKS = Counter("org_domain_checks", "DNS checks of a domain TXT record (counter, _total)", ["result"])

# --- event loop ---
EVENT_LOOP_LAG = Gauge("org_event_loop_lag_seconds", "How late the event-loop monitor woke up (0 when the loop is idle)")

# --- membership and invites ---
JOINS = Counter("org_joins", "Join attempts by source and result (ok, or the refusal code in lower case) (counter, _total)",
                ["source", "result"])
SEAT_FULL = Counter("org_seat_full", "Joins refused because the organisation is full (SEATS_FULL or PILOT_FULL) (counter, _total)")
INVITES_CREATED = Counter("org_invites_created", "Invites created, by kind (email, csv, link) (counter, _total)", ["kind"])

# --- consumer ---
CONSUMER_LAG = Gauge("org_consumer_lag", "Messages the consumer is behind on user-events and user.deleted (sum over partitions)")
DLQ_MESSAGES = Counter("org_dlq_messages", "Messages sent to organisation.dlq (counter, _total)")

# --- billing-service calls ---
BILLING_REQUESTS = Histogram("org_billing_requests_seconds", "Duration of calls to billing-service", ["route", "result"],
                             buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0))

# --- dependencies (set by /ready) ---
DEPENDENCY_UP = Gauge("org_dependency_up", "1 if the last readiness probe of a dependency passed, 0 if it failed", ["dependency"])

# --- workers ---
WORKER_UP = Gauge("org_worker_up", "1 while a background worker is running, 0 when it is down or restarting", ["worker"])


def render_all() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
