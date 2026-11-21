"""Consumer-side dedupe: one row per (consumer, event_id). No business rules; never commits."""

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.processed_event import ProcessedEvent


class ProcessedEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, consumer: str, event_id: str, now: datetime) -> bool:
        """The FIRST statement of a handler's transaction. True: first time, go ahead. False: already processed
        (the row exists and is committed), skip. A concurrent duplicate waits on the first transaction's row lock and
        then also gets False; if the first rolled back, it gets True."""
        stmt = insert(ProcessedEvent).values(consumer=consumer, event_id=event_id, processed_at=now)
        stmt = stmt.on_conflict_do_nothing(index_elements=["consumer", "event_id"])
        return (await self.session.execute(stmt)).rowcount == 1

    async def delete_older_than(self, cutoff: datetime) -> int:
        result = await self.session.execute(delete(ProcessedEvent).where(ProcessedEvent.processed_at < cutoff))
        return result.rowcount
