"""Persistence for invites and CSV batches. No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
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
        """Single (email) invites created since `since`; CSV rows and links have their own limits."""
        stmt = select(func.count()).select_from(Invite).where(
            Invite.org_id == org_id, Invite.kind == "email", Invite.created_at >= since)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_csv_since(self, org_id: uuid.UUID, since: datetime) -> int:
        """Invites created by CSV uploads since `since` (the CSV_ROWS_PER_ORG_PER_DAY count)."""
        stmt = select(func.count()).select_from(Invite).where(
            Invite.org_id == org_id, Invite.kind == "csv", Invite.created_at >= since)
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
        """Lock the row and READ IT AFRESH: populate_existing, because the caller may already hold a stale copy of it
        in the session (accept looks the invite up by token first), and a stale `uses` would let a link overfill."""
        stmt = select(Invite).where(Invite.id == invite_id).with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_open(self, org_id: uuid.UUID, now: datetime) -> int:
        """Open email and CSV invites that can still be accepted (not accepted, not revoked, not past expiry). A link
        holds no seat, so it is not counted."""
        stmt = select(func.count()).select_from(Invite).where(
            Invite.org_id == org_id, Invite.kind != "link", Invite.accepted_at.is_(None), Invite.revoked_at.is_(None), Invite.expires_at > now)
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
        stmt = select(Invite).where(Invite.org_id == org_id, Invite.kind != "link")     # links have their own list
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
        stmt = update(Invite).where(Invite.id.in_(ids)).values(email=None, invitee_name=None, purged_at=now)
        return (await self.session.execute(stmt)).rowcount

    # ---- CSV uploads (bulk)
    async def close_expired_for_emails(self, org_id: uuid.UUID, emails: list[str], now: datetime) -> int:
        """The H4 rule for many emails at once: this org's open invites past expires_at are closed as expired."""
        total = 0
        for i in range(0, len(emails), 1000):
            stmt = (
                update(Invite)
                .where(Invite.org_id == org_id, Invite.email.in_(emails[i:i + 1000]), Invite.kind != "link",
                       Invite.accepted_at.is_(None), Invite.revoked_at.is_(None), Invite.expires_at <= now)
                .values(revoked_at=now, revoke_reason="expired"))
            total += (await self.session.execute(stmt)).rowcount
        return total

    async def open_emails(self, org_id: uuid.UUID, emails: list[str]) -> set[str]:
        """Which of these emails already have an open invite in this org."""
        found: set[str] = set()
        for i in range(0, len(emails), 1000):
            stmt = select(Invite.email).where(
                Invite.org_id == org_id, Invite.email.in_(emails[i:i + 1000]), Invite.kind != "link",
                Invite.accepted_at.is_(None), Invite.revoked_at.is_(None))
            found.update(str(e).lower() for e in (await self.session.execute(stmt)).scalars())
        return found

    async def insert_open_ignoring_conflicts(self, rows: list[dict]) -> set[uuid.UUID]:
        """Bulk insert; a row that loses a race for ux_invites_open_email is skipped. Returns the ids that were inserted."""
        inserted: set[uuid.UUID] = set()
        if not rows:
            return inserted
        stmt = (
            insert(Invite)
            .on_conflict_do_nothing(
                index_elements=[Invite.org_id, Invite.email],
                index_where=text("kind <> 'link' AND accepted_at IS NULL AND revoked_at IS NULL"))
            .returning(Invite.id))
        # executemany form: the statement is compiled once, not once per row (a multi-VALUES insert of 2,000 rows
        # spent two seconds just compiling)
        inserted.update((await self.session.execute(stmt, rows)).scalars())
        return inserted

    async def batch_progress(self, batch_id: uuid.UUID, now: datetime) -> tuple[int, int]:
        """(sent, pending_send): invites of the batch with last_sent_at set, and live ones still waiting for their email
        (not revoked, not accepted, not past expiry)."""
        sent = func.count().filter(Invite.last_sent_at.is_not(None))
        pending = func.count().filter(
            Invite.last_sent_at.is_(None), Invite.revoked_at.is_(None), Invite.accepted_at.is_(None),
            Invite.expires_at > now)
        row = (await self.session.execute(select(sent, pending).where(Invite.batch_id == batch_id))).one()
        return row[0], row[1]

    async def finish_batches(self, now: datetime) -> int:
        """csv.batches.finish: a sending batch with no live invite waiting for its email becomes done."""
        stmt = (
            update(InviteBatch)
            .where(InviteBatch.status == "sending", ~select(Invite.id).where(
                Invite.batch_id == InviteBatch.id, Invite.last_sent_at.is_(None), Invite.revoked_at.is_(None),
                Invite.accepted_at.is_(None), Invite.expires_at > now).exists())
            .values(status="done", finished_at=now))
        return (await self.session.execute(stmt)).rowcount

    async def purge_batch_errors(self, cutoff: datetime, limit: int) -> int:
        """invites.purge: batches older than `cutoff` lose their error rows (they hold email addresses)."""
        ids = (select(InviteBatch.id).where(InviteBatch.created_at < cutoff, InviteBatch.errors != text("'[]'::jsonb"))
               .limit(limit).with_for_update(skip_locked=True))
        stmt = update(InviteBatch).where(InviteBatch.id.in_(ids)).values(errors=[])
        return (await self.session.execute(stmt)).rowcount

    # ---- links
    async def list_links(self, org_id: uuid.UUID) -> list[Invite]:
        stmt = select(Invite).where(Invite.org_id == org_id, Invite.kind == "link").order_by(
            Invite.created_at.desc(), Invite.id.desc())
        return list((await self.session.execute(stmt)).scalars())
