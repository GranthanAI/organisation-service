"""Prometheus metrics (default registry, so process metrics come for free)."""

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


def render_all() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
