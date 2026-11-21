"""Ending a membership (pulled forward from Phase 5, P5c). Phase 5 reuses `end()` unchanged; there is no join() yet.

One use case, in the CALLER's transaction: the caller owns the unit of work, so the membership change, the audit row
and the `org.member.left` outbox row commit or roll back together.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.enums import EndReason, MembershipStatus, OrgType, Role
from app.core.exceptions import NotFound
from app.events import topics
from app.events.producers import enqueue
from app.events.schemas import OrgMemberLeft
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import audit_service, membership_rules
from app.services.audit_service import Actor


@dataclass(frozen=True)
class EndResult:
    # True when this ending removed the last active owner (only user_deleted and org_closed can). The caller records
    # the `org.owner_missing` audit row for staff to fix.
    owner_missing: bool = False


async def end(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: EndReason,
    actor: Actor,
    *,
    staff: bool = False,
    clock: Clock = system_clock,
) -> EndResult:
    members = MembershipRepository(session)

    # 1. the active membership (NotFound if the user is not an active member)
    membership = await members.get_active(org_id, user_id)
    if membership is None:
        raise NotFound("Membership not found")

    # 2. serialise against joins: every join locks this same row. (An org without a seat_state row yet is locked
    #    through its organisation row instead.)
    if await SeatStateRepository(session).lock_for_update(org_id) is None:
        await OrgRepository(session).get_for_update(org_id)

    # 3. the last-owner rule
    role = Role(membership.role)
    check = membership_rules.check_can_end(role, await members.count_active_owners(org_id), reason)

    # 4. end it
    now = clock.now()
    membership.status = (MembershipStatus.LEFT if reason is EndReason.LEFT else MembershipStatus.REMOVED).value
    membership.ended_at = now
    membership.end_reason = reason.value
    await members.save(membership)

    # 5. audit
    action = constants.AUDIT_MEMBER_LEFT if reason is EndReason.LEFT else constants.AUDIT_MEMBER_REMOVED
    await audit_service.record(
        session, actor, action, "membership", membership.id,
        {"user_id": str(user_id), "role": role.value, "reason": reason.value},
        org_id=org_id, staff=staff, clock=clock)

    # 6. org.member.left, keyed by user_id (always with org_type: billing defaults a missing one to `company`)
    org = await OrgRepository(session).get(org_id)
    event = OrgMemberLeft.create(clock, org_id=str(org_id), user_id=str(user_id), role=role,
                                 org_type=OrgType(org.type), reason=reason)
    await enqueue(session, topics.ORG_MEMBER_LEFT, str(user_id), event, clock)

    return EndResult(owner_missing=check.owner_missing)
