"""Persistence for memberships. No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MembershipStatus, Role
from app.models.known_user import KnownUser
from app.models.membership import Membership

ACTIVE = MembershipStatus.ACTIVE.value


class MembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, membership: Membership) -> Membership:
        """Flushes at once, so ux_memberships_one_active_org fails here and not at commit."""
        self.session.add(membership)
        await self.session.flush()
        return membership

    async def save(self, membership: Membership) -> Membership:
        await self.session.flush()
        return membership

    async def get(self, membership_id: uuid.UUID) -> Membership | None:
        return await self.session.get(Membership, membership_id)

    async def count_active(self, org_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Membership).where(
            Membership.org_id == org_id, Membership.status == ACTIVE)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_active_owners(self, org_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Membership).where(
            Membership.org_id == org_id, Membership.status == ACTIVE, Membership.role == Role.OWNER.value)
        return (await self.session.execute(stmt)).scalar_one()

    async def get_active_for_user(self, user_id: uuid.UUID) -> Membership | None:
        stmt = select(Membership).where(Membership.user_id == user_id, Membership.status == ACTIVE)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_active(self, org_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
        stmt = select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user_id, Membership.status == ACTIVE)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active_for_org(self, org_id: uuid.UUID, limit: int | None = None) -> list[Membership]:
        stmt = select(Membership).where(Membership.org_id == org_id, Membership.status == ACTIVE)
        stmt = stmt.order_by(Membership.joined_at, Membership.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def list_active_user_ids(self, org_id: uuid.UUID, limit: int | None = None) -> list[uuid.UUID]:
        stmt = select(Membership.user_id).where(Membership.org_id == org_id, Membership.status == ACTIVE)
        stmt = stmt.order_by(Membership.joined_at, Membership.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def list_page(
        self,
        org_id: uuid.UUID,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 50,
        role: str | None = None,
        q: str | None = None,
        status: str = ACTIVE,
    ) -> list[tuple[Membership, KnownUser | None]]:
        """Keyset pagination on (joined_at, id), oldest first: pass the last row's (joined_at, id) as `after`.
        `q` matches the member's name or email from known_users (case-insensitive). Stable while members join,
        because new members sort after every existing key."""
        stmt = (
            select(Membership, KnownUser)
            .outerjoin(KnownUser, KnownUser.user_id == Membership.user_id)
            .where(Membership.org_id == org_id, Membership.status == status)
        )
        if role:
            stmt = stmt.where(Membership.role == role)
        if q:
            pattern = f"%{q.lower()}%"
            stmt = stmt.where(or_(KnownUser.full_name.ilike(pattern), KnownUser.email.ilike(pattern)))
        if after:
            stmt = stmt.where(tuple_(Membership.joined_at, Membership.id) > tuple_(*after))
        stmt = stmt.order_by(Membership.joined_at, Membership.id).limit(limit)
        return [(m, u) for m, u in (await self.session.execute(stmt)).all()]

    async def lock_active(self, org_id: uuid.UUID, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, Membership]:
        """The ACTIVE memberships of these users in this organisation, locked FOR UPDATE in id order (so two
        transactions locking the same pair always take the rows in the same order). Lock order everywhere: the
        seat_state row first, then memberships."""
        stmt = (
            select(Membership)
            .where(Membership.org_id == org_id, Membership.user_id.in_(user_ids), Membership.status == ACTIVE)
            .order_by(Membership.id)
            .with_for_update()
        )
        return {m.user_id: m for m in (await self.session.execute(stmt)).scalars()}

    async def active_orgs_for_users(self, user_ids: "list[uuid.UUID]") -> "dict[uuid.UUID, uuid.UUID]":
        """user_id -> org_id of the user's active membership, for many users at once."""
        found: dict[uuid.UUID, uuid.UUID] = {}
        for i in range(0, len(user_ids), 1000):
            stmt = select(Membership.user_id, Membership.org_id).where(
                Membership.user_id.in_(user_ids[i:i + 1000]), Membership.status == ACTIVE)
            found.update({user_id: org_id for user_id, org_id in (await self.session.execute(stmt)).all()})
        return found
