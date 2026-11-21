"""Persistence for org_domains and join_requests. No business rules; never commits."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import JoinRequest, OrgDomain


class DomainRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, domain: OrgDomain) -> OrgDomain:
        self.session.add(domain)
        await self.session.flush()
        return domain

    async def save(self, domain: OrgDomain) -> OrgDomain:
        """Flushes at once, so ux_org_domains_verified fails here when a second org verifies the same domain."""
        await self.session.flush()
        return domain

    async def get(self, org_id: uuid.UUID, domain: str) -> OrgDomain | None:
        return await self.session.get(OrgDomain, (org_id, domain))

    async def get_verified(self, domain: str) -> OrgDomain | None:
        stmt = select(OrgDomain).where(OrgDomain.domain == domain, OrgDomain.verified_at.is_not(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_org(self, org_id: uuid.UUID) -> list[OrgDomain]:
        stmt = select(OrgDomain).where(OrgDomain.org_id == org_id).order_by(OrgDomain.domain)
        return list((await self.session.execute(stmt)).scalars())

    async def delete(self, domain: OrgDomain) -> None:
        await self.session.delete(domain)
        await self.session.flush()

    async def add_join_request(self, request: JoinRequest) -> JoinRequest:
        self.session.add(request)
        await self.session.flush()
        return request

    async def get_join_request(self, request_id: uuid.UUID) -> JoinRequest | None:
        return await self.session.get(JoinRequest, request_id)

    async def list_join_requests(self, org_id: uuid.UUID, status: str | None = None) -> list[JoinRequest]:
        stmt = select(JoinRequest).where(JoinRequest.org_id == org_id)
        if status:
            stmt = stmt.where(JoinRequest.status == status)
        stmt = stmt.order_by(JoinRequest.created_at, JoinRequest.id)
        return list((await self.session.execute(stmt)).scalars())
