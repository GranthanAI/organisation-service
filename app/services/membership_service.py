"""Ending a membership (pulled forward from Phase 5, P5c). Phase 5 reuses `end()` unchanged; there is no join() yet.

One use case, in the CALLER's transaction: the caller owns the unit of work, so the membership change, the audit row
and the `org.member.left` outbox row commit or roll back together.
"""

import uuid
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.enums import EndReason, MembershipStatus, OrgType, Role
from app.core.exceptions import ForbiddenRole, InvalidTransferTarget, NotFound, ValidationFailed
from app.events import topics
from app.events.producers import enqueue
from app.events.schemas import OrgMemberLeft
from app.models.membership import Membership
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import OrgContext
from app.services import audit_service, membership_rules, org_gate, permission_rules
from app.services.audit_service import Actor
from app.services.permission_rules import Action


@dataclass(frozen=True)
class EndResult:
    # True when this ending removed the last active owner (only user_deleted and org_closed can). The caller records
    # the `org.owner_missing` audit row for staff to fix.
    owner_missing: bool = False


async def lock_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Take the organisation's lock: its `seat_state` row FOR UPDATE (an org without one is locked through its
    `organisations` row). LOCK ORDER everywhere (join, end, change_role, transfer_ownership, remove, leave): this row
    FIRST, then memberships (in id order). One order means no deadlocks."""
    if await SeatStateRepository(session).lock_for_update(org_id) is None:
        await OrgRepository(session).get_for_update(org_id)


async def lock_actor_and_target(session: AsyncSession, actor: OrgContext, target_user_id: uuid.UUID):
    """After `lock_org`: lock the caller's and the target's ACTIVE memberships together, in id order, and return
    (ctx with the caller's current role, target membership or None). The role the request carried may be stale (a
    concurrent ownership transfer), so every check uses the one read under the lock."""
    locked = await MembershipRepository(session).lock_active(actor.org_id, [actor.user.id, target_user_id])
    me = locked.get(actor.user.id)
    if me is None:
        raise NotFound("Organisation not found")                   # the caller left or was removed meanwhile
    return replace(actor, role=Role(me.role)), locked.get(target_user_id)


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

    # 2. serialise against joins: every join locks this same row
    await lock_org(session, org_id)

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


# ---------------------------------------------------------------------------------------------------- change_role
async def change_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    new_role: Role,
    actor: OrgContext,
    *,
    clock: Clock = system_clock,
) -> Membership:
    """Make an admin a member or a member an admin (an owner only: the matrix). No Kafka event: billing does not read
    the role. Refused: ownership (use transfer), your own role (403), a closed org. The same role is a no-op."""
    await lock_org(session, org_id)
    await org_gate.require_open(session, org_id)
    members = MembershipRepository(session)
    if new_role is Role.OWNER:
        raise ValidationFailed("ownership moves only through transfer-ownership")
    if user_id == actor.user.id:
        raise ForbiddenRole("you cannot change your own role")
    actor, target = await lock_actor_and_target(session, actor, user_id)
    if target is None:
        raise NotFound("Member not found")
    permission_rules.check(Action.CHANGE_ROLE, actor.role)           # still allowed to, after any transfer?
    target_role = Role(target.role)
    actor.check_target(target_role)
    if target_role is Role.OWNER:
        raise ValidationFailed("an owner's role changes only through transfer-ownership")
    if target_role is new_role:
        return target                                            # nothing to do: no audit
    membership_rules.check_role_change(target_role, new_role, await members.count_active_owners(org_id))
    target.role = new_role.value
    await members.save(target)
    await audit_service.record(
        session, actor.user, constants.AUDIT_MEMBER_ROLE_CHANGED, "membership", target.id,
        {"user_id": str(user_id), "from": target_role.value, "to": new_role.value}, org_id=org_id, clock=clock)
    return target


# ------------------------------------------------------------------------------------------------ transfer_ownership
async def transfer_ownership(
    session: AsyncSession,
    org_id: uuid.UUID,
    to_user_id: uuid.UUID,
    actor: OrgContext,
    *,
    clock: Clock = system_clock,
) -> tuple[Membership, Membership]:
    """The caller (the owner) becomes an admin and the target (an active ADMIN of this org) becomes the owner, in one
    transaction. Returns (new owner, old owner). Both memberships are locked after the seat_state row, in id order."""
    await lock_org(session, org_id)
    await org_gate.require_open(session, org_id)
    if to_user_id == actor.user.id:
        raise ValidationFailed("you already own this organisation")
    members = MembershipRepository(session)
    locked = await members.lock_active(org_id, [actor.user.id, to_user_id])
    me, target = locked.get(actor.user.id), locked.get(to_user_id)
    if me is None:
        raise NotFound("Organisation not found")
    if target is None:
        raise InvalidTransferTarget("the new owner must be an active member of the organisation")
    plan = membership_rules.plan_transfer(Role(me.role), Role(target.role), MembershipStatus(target.status))
    me.role, target.role = plan.old_owner_role.value, plan.new_owner_role.value
    await members.save(me)
    await members.save(target)
    for row, old, new in ((target, Role.ADMIN, Role.OWNER), (me, Role.OWNER, Role.ADMIN)):
        await audit_service.record(
            session, actor.user, constants.AUDIT_MEMBER_ROLE_CHANGED, "membership", row.id,
            {"user_id": str(row.user_id), "from": old.value, "to": new.value, "cause": "ownership transfer"},
            org_id=org_id, clock=clock)
    await audit_service.record(
        session, actor.user, constants.AUDIT_ORG_OWNERSHIP_TRANSFERRED, "org", org_id,
        {"from_user_id": str(actor.user.id), "to_user_id": str(to_user_id)}, org_id=org_id, clock=clock)
    return target, me
