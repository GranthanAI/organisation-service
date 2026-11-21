"""Persistence for the outbox. Rows are written in the same transaction as the change they describe; the relay
(workers/outbox_worker.py) publishes them. No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.outbox import OutboxEvent


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, event: OutboxEvent) -> OutboxEvent:
        self.session.add(event)
        await self.session.flush()
        return event

    async def get(self, event_id: uuid.UUID) -> OutboxEvent | None:
        return await self.session.get(OutboxEvent, event_id)

    async def count_unpublished(self) -> int:
        stmt = select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one()

    async def claim_due(self, now: datetime, limit: int = 100) -> list[OutboxEvent]:
        """Unpublished rows due now, locked with FOR UPDATE SKIP LOCKED so two workers never take the same row."""
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None), OutboxEvent.next_attempt_at <= now)
            .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def oldest_unpublished(self, limit: int = 100, now: datetime | None = None) -> list[OutboxEvent]:
        """The oldest unpublished rows in publish order (created_at, id), locked FOR UPDATE.

        With `now`, every row whose KEY has an unpublished row still backing off (next_attempt_at > now) is left out,
        so the batch is always filled with keys that can be published and a few stuck keys at the head cannot starve
        the rest. A key's rows come back again as soon as its stuck row is due for a retry."""
        stmt = select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
        if now is not None:
            blocker = aliased(OutboxEvent)
            stmt = stmt.where(~exists().where(
                blocker.key == OutboxEvent.key, blocker.published_at.is_(None), blocker.next_attempt_at > now))
        stmt = stmt.order_by(OutboxEvent.created_at, OutboxEvent.id).limit(limit).with_for_update(of=OutboxEvent)
        return list((await self.session.execute(stmt)).scalars())

    async def stats(self, now: datetime) -> tuple[int, float]:
        """(unpublished rows, age in seconds of the oldest one; 0 when none)."""
        stmt = select(func.count(), func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None))
        count, oldest = (await self.session.execute(stmt)).one()
        return count, (max(0.0, (now - oldest).total_seconds()) if oldest else 0.0)

    async def delete_published_before(self, cutoff: datetime) -> int:
        result = await self.session.execute(delete(OutboxEvent).where(OutboxEvent.published_at < cutoff))
        return result.rowcount
