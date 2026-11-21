"""Persistence for the outbox. Rows are written in the same transaction as the change they describe. The publishing
worker is Phase 4; `claim_due` is the query it will use. No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
