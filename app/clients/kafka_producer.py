"""Kafka connectivity probe. The producer itself arrives in Phase 4; Phase 0 only checks the brokers answer."""

import asyncio

from aiokafka.client import AIOKafkaClient

from app.core.config import settings


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
