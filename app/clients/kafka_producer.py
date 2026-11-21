"""Kafka producer (aiokafka) and the readiness probe.

`EventProducer` is the small interface the outbox relay and the consumer's DLQ use, so tests pass a fake.
The real producer waits for the broker's ack (`acks="all"`, idempotence on, linger 5 ms) and tags every message
with the correlation_id header when there is one.
"""

import asyncio
import json
from typing import Any, Protocol

from aiokafka import AIOKafkaProducer
from aiokafka.client import AIOKafkaClient

from app.core.config import settings


class EventProducer(Protocol):
    async def send(self, topic: str, key: str, value: dict[str, Any],
                   correlation_id: str | None = None) -> None:
        """Return only after the broker acknowledged the message; raise otherwise."""


def build_headers(correlation_id: str | None) -> list[tuple[str, bytes]] | None:
    return [("correlation_id", correlation_id.encode("utf-8"))] if correlation_id else None


class KafkaEventProducer:
    def __init__(self, bootstrap_servers: str | None = None) -> None:
        self._bootstrap = bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        if self._producer is not None:
            return
        producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
            value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8"),
        )
        await producer.start()
        self._producer = producer

    async def stop(self) -> None:
        if self._producer is not None:
            producer, self._producer = self._producer, None
            await producer.stop()

    async def send(self, topic: str, key: str, value: dict[str, Any], correlation_id: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka producer is not started")
        await self._producer.send_and_wait(topic, value=value, key=key, headers=build_headers(correlation_id))


async def check_kafka() -> bool:
    """Bootstrap against the brokers and close again; True if they answered in time."""
    client = AIOKafkaClient(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                            request_timeout_ms=int(settings.READY_PROBE_TIMEOUT_SECONDS * 1000))
    try:
        await asyncio.wait_for(client.bootstrap(), timeout=settings.READY_PROBE_TIMEOUT_SECONDS + 1)
        return True
    except Exception:
        return False
    finally:
        try:
            await client.close()
        except Exception:
            pass
