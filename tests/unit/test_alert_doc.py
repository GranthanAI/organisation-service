"""docs/alerts.md must not drift from the code: every org_* metric it names exists in app/telemetry, and every runbook section it
points to is a heading in docs/runbook.md."""

import re
from pathlib import Path

from prometheus_client import REGISTRY

import app.telemetry.metrics  # noqa: F401  (registers everything)

DOCS = Path(__file__).resolve().parents[2] / "docs"
ALERTS = (DOCS / "alerts.md").read_text(encoding="utf-8")
RUNBOOK = (DOCS / "runbook.md").read_text(encoding="utf-8")


def metric_names(text: str) -> set[str]:
    return set(re.findall(r"`(org_[a-z0-9_]+)", text)) | set(re.findall(r"\b(org_[a-z0-9_]+?)(?:_bucket|_count|_sum)?\{", text))


def registered() -> set[str]:
    return set(REGISTRY._names_to_collectors)


def test_every_metric_in_the_alert_doc_exists():
    named = metric_names(ALERTS)
    assert len(named) >= 10, named
    missing = {n for n in named if n not in registered() and not any(n.startswith(r) for r in registered() if r.startswith("org_"))}
    assert missing == set(), f"docs/alerts.md names metrics that do not exist: {sorted(missing)}"


def test_the_exact_names_exist_not_just_prefixes():
    exact = {n for n in metric_names(ALERTS) if not n.endswith(("_bucket", "_count", "_sum"))}
    assert exact <= registered(), sorted(exact - registered())


def test_the_alerts_the_prompt_requires_are_all_there():
    required = ["org_outbox_oldest_age_seconds", "org_outbox_blocked_keys", "org_consumer_lag", "org_dlq_messages_total",
                "org_internal_requests_seconds", "org_event_loop_lag_seconds", "org_jobs_failed_total", "org_email_deferred_total",
                "org_billing_requests_seconds", "org_dependency_up"]
    for name in required:
        assert name in ALERTS, name


def test_every_runbook_section_an_alert_points_to_is_a_heading():
    rows = [line for line in ALERTS.splitlines() if re.match(r"\| \d+ \|", line)]
    assert len(rows) >= 10
    headings = {h.strip().lower() for h in re.findall(r"^#+ (.+)$", RUNBOOK, flags=re.M)}
    for row in rows:
        section = [c.strip() for c in row.strip("|").split("|")][-1]
        assert section.lower() in headings, f"alert row points to a runbook section that does not exist: {section!r}"


def test_the_metrics_the_code_promises_are_registered():
    for name in ("org_joins_total", "org_seat_full_total", "org_invites_created_total", "org_internal_requests_seconds",
                 "org_consumer_lag", "org_outbox_oldest_age_seconds", "org_jobs_failed_total", "org_email_sent_total",
                 "org_email_deferred_total", "org_event_loop_lag_seconds", "org_billing_requests_seconds"):
        assert name in registered(), name
