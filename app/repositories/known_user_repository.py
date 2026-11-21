"""Persistence for known_users (built from auth-service events). No business rules; never commits."""

import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.known_user import KnownUser

PLACEHOLDER_DOMAIN = "deleted.invalid"


class KnownUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> KnownUser | None:
        await self.session.flush()
        return await self.session.get(KnownUser, user_id, populate_existing=True)

    async def get_by_email(self, email: str) -> KnownUser | None:
        stmt = select(KnownUser).where(KnownUser.email == email).order_by(KnownUser.updated_at.desc()).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(self, user_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, KnownUser]:
        if not user_ids:
            return {}
        stmt = select(KnownUser).where(KnownUser.user_id.in_(list(user_ids)))
        return {u.user_id: u for u in (await self.session.execute(stmt)).scalars()}

    async def upsert(self, user_id: uuid.UUID, email: str, full_name: str | None, now: datetime) -> None:
        """Insert, or refresh email and name. Never clears email_verified_at or deleted_at."""
        stmt = insert(KnownUser).values(user_id=user_id, email=email, full_name=full_name, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=[KnownUser.user_id],
            set_={"email": stmt.excluded.email, "full_name": stmt.excluded.full_name, "updated_at": now},
        )
        await self.session.execute(stmt)

    async def set_email_verified(self, user_id: uuid.UUID, email: str, verified_at: datetime,
                                 now: datetime | None = None) -> None:
        """Record that the email is verified. Keeps the EARLIEST verification time (LEAST ignores NULL), updates the
        email, and creates the row if the user is not known yet."""
        now = now or verified_at
        stmt = insert(KnownUser).values(user_id=user_id, email=email, email_verified_at=verified_at, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=[KnownUser.user_id],
            set_={
                "email": stmt.excluded.email,
                "email_verified_at": func.least(KnownUser.email_verified_at, stmt.excluded.email_verified_at),
                "updated_at": now,
            },
        )
        await self.session.execute(stmt)

    async def mark_deleted(self, user_id: uuid.UUID, now: datetime) -> None:
        """Set deleted_at (keeping the first time), creating the row if the user was never seen. A created row has
        a placeholder `<id>@deleted.invalid` email, because user.deleted carries none."""
        stmt = insert(KnownUser).values(user_id=user_id, email=f"{user_id}@{PLACEHOLDER_DOMAIN}", deleted_at=now,
                                        updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=[KnownUser.user_id],
            set_={"deleted_at": func.coalesce(KnownUser.deleted_at, stmt.excluded.deleted_at), "updated_at": now},
        )
        await self.session.execute(stmt)

    async def set_deleted(self, user_id: uuid.UUID, deleted_at: datetime) -> None:
        user = await self.get(user_id)
        if user is not None:
            user.deleted_at = deleted_at
            user.updated_at = deleted_at
            await self.session.flush()
