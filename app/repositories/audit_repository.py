"""Persistence for audit_log. INSERT-ONLY: there is deliberately no update or delete method (the table also has a
trigger that rejects both). Reads are newest first."""

import uuid
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: AuditLog) -> AuditLog:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_page(
        self,
        org_id: uuid.UUID,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 50,
        action: str | None = None,
    ) -> list[AuditLog]:
        """Newest first, keyset on (at, id): pass the last row's (at, id) as `after`."""
        stmt = select(AuditLog).where(AuditLog.org_id == org_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if after:
            stmt = stmt.where(tuple_(AuditLog.at, AuditLog.id) < tuple_(*after))
        stmt = stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())
