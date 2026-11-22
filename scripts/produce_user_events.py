"""Produce auth-service style UserRegistered and EmailVerified messages to the dev Kafka (envelope {event_type, payload,
created_at} on `user-events`, keyed by user_id), as auth-service's outbox worker does. For live checks only: it stands in for
auth-service, which is never edited or run for this.

    uv run python -m scripts.produce_user_events registered <user_id> <email> [full name]
    uv run python -m scripts.produce_user_events verified   <user_id> <email>
    uv run python -m scripts.produce_user_events both       <user_id> <email> [full name]
"""

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer

from app.core.config import settings
from app.events import topics


def envelope(event_type: str, user_id: str, email: str, full_name: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    payload = {"event_id": str(uuid.uuid4()), "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "user_id": user_id, "email": email}
    if event_type == "UserRegistered":
        payload["full_name"] = full_name
    return {"event_type": event_type, "payload": payload, "created_at": now.isoformat()}


async def main(kind: str, user_id: str, email: str, full_name: str | None) -> None:
    uuid.UUID(user_id)
    events = []
    if kind in ("registered", "both"):
        events.append(envelope("UserRegistered", user_id, email, full_name))
    if kind in ("verified", "both"):
        events.append(envelope("EmailVerified", user_id, email))
    producer = AIOKafkaProducer(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS, acks="all")
    await producer.start()
    try:
        for event in events:
            await producer.send_and_wait(topics.USER_EVENTS, json.dumps(event).encode(), key=user_id.encode())
            print(json.dumps({"produced": event["event_type"], "user_id": user_id, "email": email}))
    finally:
        await producer.stop()


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] not in ("registered", "verified", "both"):
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]) or None))
