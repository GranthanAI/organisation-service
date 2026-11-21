"""Write events to the outbox. This is the ONLY way application code publishes.

`enqueue` validates the event and inserts an outbox row in the CALLER's session: it never publishes and never
commits, so the event exists if and only if the change that caused it commits. The relay (workers/outbox_worker.py)
does the publishing.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, system_clock
from app.events.schemas import PRODUCED, ProducedEvent
from app.middleware.correlation import get_correlation_id
from app.models.outbox import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository
from app.utils.ids import uuid7


async def enqueue(session: AsyncSession, topic: str, key: str, event: ProducedEvent,
                  clock: Clock = system_clock) -> OutboxEvent:
    """Raises ValueError for an unknown topic, a model that does not belong to the topic, or a key that is not the
    field the topic is keyed by (member events: user_id; the rest: org_id)."""
    if topic not in PRODUCED:
        raise ValueError(f"{topic!r} is not a topic this service produces")
    model, key_field = PRODUCED[topic]
    if type(event) is not model:
        raise ValueError(f"{topic} carries {model.__name__}, not {type(event).__name__}")
    payload = model.model_validate(event.model_dump()).model_dump(mode="json")   # re-validated, as plain JSON
    if key != payload[key_field]:
        raise ValueError(f"{topic} must be keyed by {key_field}")
    now = clock.now()
    row = OutboxEvent(id=uuid7(), topic=topic, key=key, payload=payload, created_at=now, next_attempt_at=now,
                      correlation_id=get_correlation_id() or None)
    return await OutboxRepository(session).add(row)
