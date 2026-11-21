"""Prometheus metrics (default registry, so process metrics come for free)."""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

# --- outbox relay ---
OUTBOX_PENDING = Gauge("org_outbox_pending", "Outbox rows not yet published")
OUTBOX_OLDEST_AGE = Gauge("org_outbox_oldest_age_seconds", "Age of the oldest unpublished outbox row (0 when empty)")
OUTBOX_PUBLISH_FAILURES = Counter("org_outbox_publish_failures", "Outbox publishes that failed (counter, _total)")

# --- jobs ---
JOBS_FAILED = Counter("org_jobs_failed", "Jobs given up on after their last attempt", ["kind"])

# --- workers ---
WORKER_UP = Gauge("org_worker_up", "1 while a background worker is running, 0 when it is down or restarting", ["worker"])


def render_all() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
