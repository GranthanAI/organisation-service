"""Persistence for seat_state, the row locked on every join. No business rules; never commits."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seat_state import SeatState


class SeatStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, seat_state: SeatState) -> SeatState:
        self.session.add(seat_state)
        await self.session.flush()
        return seat_state

    async def save(self, seat_state: SeatState) -> SeatState:
        await self.session.flush()
        return seat_state

    async def get(self, org_id: uuid.UUID) -> SeatState | None:
        return await self.session.get(SeatState, org_id)

    async def lock_for_update(self, org_id: uuid.UUID) -> SeatState | None:
        """SELECT ... FOR UPDATE: a second transaction locking the same org waits until this one ends."""
        stmt = select(SeatState).where(SeatState.org_id == org_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()
