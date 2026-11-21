"""The organisation status gate for writes (Phase 6), ONE helper used by every write.

  closed:                      every write is refused (ORG_INACTIVE), leave included: the close job ends everyone;
  suspended, pilot_expired:    role changes, removals, detail edits and leave are still allowed (an admin may tidy up);
  pilot, active:               everything is allowed.
Reads are allowed in every status, so members can see why access stopped. The system's own endings (the close job,
user deletion) do not go through this gate.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OrgStatus
from app.core.exceptions import NotFound, OrgInactive
from app.models.organisation import Organisation


async def require_open(session: AsyncSession, org_id: uuid.UUID) -> str:
    """Return the organisation status, or raise: NotFound if it does not exist, OrgInactive if it is closed."""
    status = (await session.execute(select(Organisation.status).where(Organisation.id == org_id))).scalar_one_or_none()
    if status is None:
        raise NotFound("Organisation not found")
    if status == OrgStatus.CLOSED.value:
        raise OrgInactive("the organisation is closed")
    return status
