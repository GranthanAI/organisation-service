"""What billing-service asks us (docs/PHASES.md Contracts 1): who is in which org, the org details, its members.

Each function is one cheap query on a read session. Billing grants the organisation plan only when the membership
status it reads is "active", so the status reported here is "active" while the organisation can take members (pilot or
active), and the organisation's own status otherwise (suspended, pilot_expired, closed): a suspended or expired
organisation stops granting its plan, while its members stay members.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MembershipStatus, OrgStatus
from app.core.exceptions import NotFound, ValidationFailed
from app.core.logging import logger
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.policy import OrgPolicy
from app.repositories.membership_repository import MembershipRepository

MEMBERS_MAX = 10_000
GRANTING = frozenset({OrgStatus.PILOT.value, OrgStatus.ACTIVE.value})


def reported_status(org_status: str) -> str:
    """The status of an ACTIVE membership as billing should read it."""
    return "active" if org_status in GRANTING else org_status


async def membership_for_user(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """The user's ACTIVE membership as billing reads it; NotFound (404) if they are in no organisation."""
    stmt = (
        select(Membership.org_id, Membership.role, Organisation.type, Organisation.status)
        .join(Organisation, Organisation.id == Membership.org_id)
        .where(Membership.user_id == user_id, Membership.status == MembershipStatus.ACTIVE.value)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFound("No organisation")
    org_id, role, org_type, org_status = row
    return {"org_id": org_id, "org_type": org_type, "role": role, "status": reported_status(org_status)}


async def org_details(session: AsyncSession, org_id: uuid.UUID) -> dict:
    """The organisation (closed ones too: billing may still need them for invoices); NotFound (404) if unknown."""
    stmt = (
        select(Organisation, OrgPolicy.member_spend_cap_paise)
        .outerjoin(OrgPolicy, OrgPolicy.org_id == Organisation.id)
        .where(Organisation.id == org_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFound("Organisation not found")
    org, cap = row
    return {"org_id": org.id, "name": org.name, "type": org.type, "gstin": org.gstin,
            "billing_email": org.billing_email, "state_code": org.state_code, "member_spend_cap_paise": cap}


async def member_ids(session: AsyncSession, org_id: uuid.UUID) -> dict:
    """Active members only, oldest first, in one response with no paging. Billing uses it for the pilot check (at most
    25); above 10,000 it answers 422 and logs, because nothing legitimate needs that many in one call."""
    ids = await MembershipRepository(session).list_active_user_ids(org_id, limit=MEMBERS_MAX + 1)
    if len(ids) > MEMBERS_MAX:
        logger.error("internal members list is over the limit", org_id=str(org_id), limit=MEMBERS_MAX)
        raise ValidationFailed(f"the organisation has more than {MEMBERS_MAX} members")
    if not ids and (await session.get(Organisation, org_id)) is None:
        raise NotFound("Organisation not found")
    return {"user_ids": ids}
