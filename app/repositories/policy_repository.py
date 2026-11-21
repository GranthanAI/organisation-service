"""Persistence for org_policies. No business rules; never commits."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.policy import OrgPolicy


class PolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, policy: OrgPolicy) -> OrgPolicy:
        self.session.add(policy)
        await self.session.flush()
        return policy

    async def get(self, org_id: uuid.UUID) -> OrgPolicy | None:
        await self.session.flush()
        return await self.session.get(OrgPolicy, org_id, populate_existing=True)

    async def update_if_version(
        self, org_id: uuid.UUID, expected_version: int, updated_by: uuid.UUID | None, updated_at: datetime,
        **fields: Any,
    ) -> bool:
        """Optimistic update for If-Match: applies `fields` and bumps `version` only if the row is still at
        `expected_version`. Returns False on a stale version (the caller answers VERSION_CONFLICT)."""
        stmt = (
            update(OrgPolicy)
            .where(OrgPolicy.org_id == org_id, OrgPolicy.version == expected_version)
            .values(version=OrgPolicy.version + 1, updated_by=updated_by, updated_at=updated_at, **fields)
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1
