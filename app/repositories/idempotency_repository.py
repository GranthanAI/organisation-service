"""Persistence for idempotency_keys. Scoped to (actor_id, route, key). No business rules; never commits."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency_key import IdempotencyKey


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve(self, actor_id: uuid.UUID, route: str, key: str, request_hash: str, now: datetime,
                      locked_until: datetime) -> bool:
        """Insert an in_progress reservation. False if the (actor, route, key) row already exists."""
        stmt = insert(IdempotencyKey).values(
            actor_id=actor_id, route=route, key=key, request_hash=request_hash, state="in_progress",
            locked_until=locked_until, created_at=now,
        ).on_conflict_do_nothing(index_elements=["actor_id", "route", "key"])
        return (await self.session.execute(stmt)).rowcount == 1

    async def get(self, actor_id: uuid.UUID, route: str, key: str) -> IdempotencyKey | None:
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.actor_id == actor_id, IdempotencyKey.route == route, IdempotencyKey.key == key)
        return (await self.session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()

    async def take_over(self, actor_id: uuid.UUID, route: str, key: str, request_hash: str, now: datetime,
                        locked_until: datetime, expired_before: datetime) -> bool:
        """Atomically restart a reservation whose lock has passed, or a row older than the retention window.
        Returns True if this caller now holds it (only one concurrent caller can win)."""
        stmt = (
            update(IdempotencyKey)
            .where(
                IdempotencyKey.actor_id == actor_id, IdempotencyKey.route == route, IdempotencyKey.key == key,
                or_(
                    and_(IdempotencyKey.state == "in_progress", IdempotencyKey.locked_until <= now),
                    IdempotencyKey.created_at <= expired_before,
                ),
            )
            .values(request_hash=request_hash, state="in_progress", locked_until=locked_until, created_at=now,
                    status_code=None, response=None)
        )
        return (await self.session.execute(stmt)).rowcount == 1

    async def complete(self, actor_id: uuid.UUID, route: str, key: str, status_code: int, response: Any) -> None:
        stmt = (
            update(IdempotencyKey)
            .where(IdempotencyKey.actor_id == actor_id, IdempotencyKey.route == route, IdempotencyKey.key == key)
            .values(state="done", status_code=status_code, response=response, locked_until=None)
        )
        await self.session.execute(stmt)

    async def delete(self, actor_id: uuid.UUID, route: str, key: str) -> None:
        await self.session.execute(delete(IdempotencyKey).where(
            IdempotencyKey.actor_id == actor_id, IdempotencyKey.route == route, IdempotencyKey.key == key))
