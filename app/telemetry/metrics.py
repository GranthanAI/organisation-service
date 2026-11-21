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

# --- workers ---
WORKER_UP = Gauge("org_worker_up", "1 while a background worker is running, 0 when it is down or restarting", ["worker"])


def render_all() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
