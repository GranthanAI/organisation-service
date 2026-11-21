"""Persistence for organisations. No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organisation import Organisation


class OrgRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, org: Organisation) -> Organisation:
        self.session.add(org)
        await self.session.flush()
        return org

    async def save(self, org: Organisation) -> Organisation:
        await self.session.flush()
        return org

    async def get(self, org_id: uuid.UUID) -> Organisation | None:
        return await self.session.get(Organisation, org_id)

    async def get_for_update(self, org_id: uuid.UUID) -> Organisation | None:
        result = await self.session.execute(
            select(Organisation).where(Organisation.id == org_id).with_for_update())
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Organisation | None:
        result = await self.session.execute(select(Organisation).where(Organisation.slug == slug))
        return result.scalar_one_or_none()

    async def search(
        self,
        q: str | None = None,
        status: str | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 50,
    ) -> list[Organisation]:
        """Newest first, keyset on (created_at, id). `q` matches name or slug (case-insensitive)."""
        stmt = select(Organisation)
        if q:
            pattern = f"%{q.lower()}%"
            stmt = stmt.where(or_(Organisation.name.ilike(pattern), Organisation.slug.ilike(pattern)))
        if status:
            stmt = stmt.where(Organisation.status == status)
        if after:
            stmt = stmt.where(tuple_(Organisation.created_at, Organisation.id) < tuple_(*after))
        stmt = stmt.order_by(Organisation.created_at.desc(), Organisation.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())
