"""Persistence for invites and CSV batches. No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invite import Invite, InviteBatch


class InviteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, invite: Invite) -> Invite:
        """Flushes at once, so ux_invites_open_email fails here."""
        self.session.add(invite)
        await self.session.flush()
        return invite

    async def save(self, invite: Invite) -> Invite:
        await self.session.flush()
        return invite

    async def get(self, invite_id: uuid.UUID) -> Invite | None:
        return await self.session.get(Invite, invite_id)

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        stmt = select(Invite).where(Invite.token_hash == token_hash)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_token_hash_for_update(self, token_hash: str) -> Invite | None:
        stmt = select(Invite).where(Invite.token_hash == token_hash).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_open_for_email(self, org_id: uuid.UUID, email: str) -> Invite | None:
        stmt = select(Invite).where(
            Invite.org_id == org_id, Invite.email == email, Invite.kind != "link",
            Invite.accepted_at.is_(None), Invite.revoked_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self,
        org_id: uuid.UUID,
        kind: str | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 50,
    ) -> list[Invite]:
        """Newest first, keyset on (created_at, id)."""
        stmt = select(Invite).where(Invite.org_id == org_id)
        if kind:
            stmt = stmt.where(Invite.kind == kind)
        if after:
            stmt = stmt.where(tuple_(Invite.created_at, Invite.id) < tuple_(*after))
        stmt = stmt.order_by(Invite.created_at.desc(), Invite.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def count_created_since(self, org_id: uuid.UUID, since: datetime) -> int:
        stmt = select(func.count()).select_from(Invite).where(Invite.org_id == org_id, Invite.created_at >= since)
        return (await self.session.execute(stmt)).scalar_one()

    async def add_batch(self, batch: InviteBatch) -> InviteBatch:
        self.session.add(batch)
        await self.session.flush()
        return batch

    async def get_batch(self, batch_id: uuid.UUID) -> InviteBatch | None:
        return await self.session.get(InviteBatch, batch_id)

    async def save_batch(self, batch: InviteBatch) -> InviteBatch:
        await self.session.flush()
        return batch

    async def revoke_open_for_email(self, email: str, now: datetime, reason: str) -> int:
        """Close every open (not accepted, not revoked) email or CSV invite to `email`, in every organisation."""
        stmt = (
            update(Invite)
            .where(Invite.email == email, Invite.kind != "link", Invite.accepted_at.is_(None),
                   Invite.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason=reason)
        )
        return (await self.session.execute(stmt)).rowcount

    async def revoke_open_for_org(self, org_id: uuid.UUID, now: datetime, reason: str) -> int:
        """Close every open invite of an organisation (email, CSV and link), e.g. when it is closed."""
        stmt = (
            update(Invite)
            .where(Invite.org_id == org_id, Invite.accepted_at.is_(None), Invite.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason=reason)
        )
        return (await self.session.execute(stmt)).rowcount

    async def get_for_update(self, invite_id: uuid.UUID) -> Invite | None:
        stmt = select(Invite).where(Invite.id == invite_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_open(self, org_id: uuid.UUID, now: datetime) -> int:
        """Open invites that can still be accepted (not accepted, not revoked, not past expiry)."""
        stmt = select(func.count()).select_from(Invite).where(
            Invite.org_id == org_id, Invite.accepted_at.is_(None), Invite.revoked_at.is_(None), Invite.expires_at > now)
        return (await self.session.execute(stmt)).scalar_one()

    async def close_expired_for_email(self, org_id: uuid.UUID, email: str, now: datetime) -> int:
        """The H4 rule, lazily: this org's open invite for `email` that is past expires_at is closed as expired."""
        stmt = (
            update(Invite)
            .where(Invite.org_id == org_id, Invite.email == email, Invite.kind != "link", Invite.accepted_at.is_(None),
                   Invite.revoked_at.is_(None), Invite.expires_at <= now)
            .values(revoked_at=now, revoke_reason="expired")
        )
        return (await self.session.execute(stmt)).rowcount

    async def list_for_org(self, org_id: uuid.UUID, status: str | None, now: datetime,
                           after: tuple[datetime, uuid.UUID] | None, limit: int) -> list[Invite]:
        """Newest first, keyset on (created_at, id). `status` is open, accepted, expired or revoked, as computed:
        an open invite past expires_at is `expired`, and so is one the expiry job closed."""
        stmt = select(Invite).where(Invite.org_id == org_id)
        is_open = and_(Invite.accepted_at.is_(None), Invite.revoked_at.is_(None))
        expired = or_(and_(is_open, Invite.expires_at <= now),
                      and_(Invite.revoked_at.is_not(None), Invite.revoke_reason == "expired", Invite.accepted_at.is_(None)))
        if status == "open":
            stmt = stmt.where(is_open, Invite.expires_at > now)
        elif status == "accepted":
            stmt = stmt.where(Invite.accepted_at.is_not(None))
        elif status == "expired":
            stmt = stmt.where(expired)
        elif status == "revoked":
            stmt = stmt.where(Invite.revoked_at.is_not(None), Invite.accepted_at.is_(None),
                              or_(Invite.revoke_reason.is_(None), Invite.revoke_reason != "expired"))
        if after:
            stmt = stmt.where(tuple_(Invite.created_at, Invite.id) < tuple_(*after))
        stmt = stmt.order_by(Invite.created_at.desc(), Invite.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def expire_open_before(self, now: datetime, limit: int) -> int:
        """invites.expire: close up to `limit` open invites past expires_at (revoke_reason 'expired')."""
        ids = (select(Invite.id).where(Invite.accepted_at.is_(None), Invite.revoked_at.is_(None), Invite.expires_at <= now)
               .order_by(Invite.expires_at).limit(limit).with_for_update(skip_locked=True))
        stmt = update(Invite).where(Invite.id.in_(ids)).values(revoked_at=now, revoke_reason="expired")
        return (await self.session.execute(stmt)).rowcount

    async def purge_before(self, cutoff: datetime, now: datetime, limit: int) -> int:
        """invites.purge: never-accepted invites created before `cutoff` lose their email (data minimisation)."""
        ids = (select(Invite.id).where(Invite.accepted_at.is_(None), Invite.created_at < cutoff,
                                       Invite.purged_at.is_(None), Invite.email.is_not(None))
               .order_by(Invite.created_at).limit(limit).with_for_update(skip_locked=True))
        stmt = update(Invite).where(Invite.id.in_(ids)).values(email=None, purged_at=now)
        return (await self.session.execute(stmt)).rowcount
