"""Shared FastAPI dependencies."""

from collections.abc import Awaitable, Callable

from app.clients.kafka_producer import check_kafka
from app.clients.redis_client import check_redis
from app.db.postgres import check_database

Probe = Callable[[], Awaitable[bool]]


def get_probes() -> dict[str, Probe]:
    """Readiness probes by dependency name. Tests override this dependency with fakes."""
    return {"postgres": check_database, "redis": check_redis, "kafka": check_kafka}
