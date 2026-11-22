"""Persistence for org_domains and join_requests. No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import JoinRequest, OrgDomain
from app.services.domain_rules import candidate_domains


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

    async def get_for_update(self, org_id: uuid.UUID, domain: str) -> OrgDomain | None:
        stmt = (select(OrgDomain).where(OrgDomain.org_id == org_id, OrgDomain.domain == domain)
                .with_for_update().execution_options(populate_existing=True))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_verified(self, domain: str) -> OrgDomain | None:
        stmt = select(OrgDomain).where(OrgDomain.domain == domain, OrgDomain.verified_at.is_not(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_org(self, org_id: uuid.UUID) -> list[OrgDomain]:
        stmt = select(OrgDomain).where(OrgDomain.org_id == org_id).order_by(OrgDomain.domain)
        return list((await self.session.execute(stmt)).scalars())

    async def count_for_org(self, org_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(OrgDomain).where(OrgDomain.org_id == org_id)
        return (await self.session.execute(stmt)).scalar_one()

    async def delete(self, domain: OrgDomain) -> None:
        await self.session.delete(domain)
        await self.session.flush()

    # ---- ownership rules
    async def verified_blocking(self, domain: str, *, include_children: bool,
                                exclude_org: uuid.UUID | None = None) -> list[OrgDomain]:
        """Verified domains (of any org, or of any org but `exclude_org`) that overlap `domain`: the same domain, a verified
        parent that includes its subdomains, and (when `include_children`) any verified subdomain of it."""
        parents = [c for c in candidate_domains(domain) if c != domain]
        clauses = [OrgDomain.domain == domain]
        if parents:
            clauses.append(and_(OrgDomain.domain.in_(parents), OrgDomain.include_subdomains.is_(True)))
        if include_children:
            clauses.append(OrgDomain.domain.like("%." + domain))
        stmt = select(OrgDomain).where(OrgDomain.verified_at.is_not(None), or_(*clauses))
        if exclude_org is not None:
            stmt = stmt.where(OrgDomain.org_id != exclude_org)
        return list((await self.session.execute(stmt)).scalars())

    async def find_verified_owner(self, email_domain: str) -> tuple[uuid.UUID, str, bool] | None:
        """Which organisation owns this email domain: (org_id, matched domain, auto_join), or None. The verified domain
        equal to it, or a verified parent with include_subdomains; the longest match wins."""
        candidates = candidate_domains(email_domain)
        if not candidates:
            return None
        stmt = select(OrgDomain).where(OrgDomain.domain.in_(candidates), OrgDomain.verified_at.is_not(None))
        best: OrgDomain | None = None
        for row in (await self.session.execute(stmt)).scalars():
            if row.domain != email_domain and not row.include_subdomains:
                continue
            if best is None or len(row.domain) > len(best.domain):
                best = row
        return None if best is None else (best.org_id, best.domain, best.auto_join)

    # ---- jobs
    async def pending_to_check(self, added_after: datetime, limit: int) -> list[OrgDomain]:
        """Pending (unverified) domains added after `added_after` (the hourly check stops after 7 days)."""
        stmt = (select(OrgDomain).where(OrgDomain.verified_at.is_(None), OrgDomain.added_at > added_after)
                .order_by(OrgDomain.added_at).limit(limit))
        return list((await self.session.execute(stmt)).scalars())

    async def dns_verified_due(self, checked_before: datetime, limit: int) -> list[OrgDomain]:
        """DNS-verified domains whose last check is older than `checked_before` (staff-verified ones are never rechecked)."""
        stmt = (select(OrgDomain).where(
            OrgDomain.verify_method == "dns", OrgDomain.verified_at.is_not(None),
            or_(OrgDomain.last_checked_at.is_(None), OrgDomain.last_checked_at < checked_before))
            .order_by(OrgDomain.last_checked_at.nulls_first()).limit(limit))
        return list((await self.session.execute(stmt)).scalars())

    # ---- join requests
    async def add_join_request(self, request: JoinRequest) -> JoinRequest:
        """Flushes at once, so ux_join_requests_pending fails here."""
        self.session.add(request)
        await self.session.flush()
        return request

    async def get_join_request(self, request_id: uuid.UUID) -> JoinRequest | None:
        return await self.session.get(JoinRequest, request_id)

    async def get_join_request_for_update(self, request_id: uuid.UUID) -> JoinRequest | None:
        stmt = (select(JoinRequest).where(JoinRequest.id == request_id).with_for_update()
                .execution_options(populate_existing=True))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def pending_request_for_user(self, user_id: uuid.UUID, org_id: uuid.UUID | None = None) -> JoinRequest | None:
        stmt = select(JoinRequest).where(JoinRequest.user_id == user_id, JoinRequest.status == "pending")
        if org_id is not None:
            stmt = stmt.where(JoinRequest.org_id == org_id)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def list_join_requests(self, org_id: uuid.UUID, status: str | None = None) -> list[JoinRequest]:
        stmt = select(JoinRequest).where(JoinRequest.org_id == org_id)
        if status:
            stmt = stmt.where(JoinRequest.status == status)
        stmt = stmt.order_by(JoinRequest.created_at, JoinRequest.id)
        return list((await self.session.execute(stmt)).scalars())

    async def list_join_requests_page(self, org_id: uuid.UUID, status: str | None,
                                      after: tuple[datetime, uuid.UUID] | None, limit: int) -> list[JoinRequest]:
        """Oldest first (the queue an admin works through), keyset on (created_at, id)."""
        stmt = select(JoinRequest).where(JoinRequest.org_id == org_id)
        if status:
            stmt = stmt.where(JoinRequest.status == status)
        if after:
            stmt = stmt.where(tuple_(JoinRequest.created_at, JoinRequest.id) > tuple_(*after))
        stmt = stmt.order_by(JoinRequest.created_at, JoinRequest.id).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def decline_pending_for_user(self, user_id: uuid.UUID, now: datetime) -> int:
        """The user joined an organisation another way: every pending request of theirs is closed (declined, no decider)."""
        stmt = (update(JoinRequest).where(JoinRequest.user_id == user_id, JoinRequest.status == "pending")
                .values(status="declined", decided_by=None, decided_at=now))
        return (await self.session.execute(stmt)).rowcount

    async def expire_pending_before(self, cutoff: datetime, now: datetime, limit: int) -> int:
        ids = (select(JoinRequest.id).where(JoinRequest.status == "pending", JoinRequest.created_at < cutoff)
               .order_by(JoinRequest.created_at).limit(limit).with_for_update(skip_locked=True))
        stmt = update(JoinRequest).where(JoinRequest.id.in_(ids)).values(status="expired", decided_at=now)
        return (await self.session.execute(stmt)).rowcount
