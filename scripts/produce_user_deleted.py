"""Produce one user.deleted message to the dev Kafka, shaped exactly like auth-service's (bare JSON, keyed by user_id).
For the live check: it stands in for auth-service, which is never edited or run for this.

    uv run python -m scripts.produce_user_deleted <user_id>
"""

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer

from app.core.config import settings


async def main(user_id: str) -> None:
    uuid.UUID(user_id)
    event = {
        "event_type": "user.deleted",
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_id": user_id,
    }
    producer = AIOKafkaProducer(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS, acks="all")
    await producer.start()
    try:
        meta = await producer.send_and_wait("user.deleted", json.dumps(event).encode(), key=user_id.encode())
    finally:
        await producer.stop()
    print(json.dumps({"produced": event, "partition": meta.partition, "offset": meta.offset}))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.produce_user_deleted <user_id>")
    asyncio.run(main(sys.argv[1]))
