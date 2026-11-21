"""The join transaction: the ONLY code that creates an active membership (invite, CSV, link, domain and staff all call
it), so the seat, pilot and one-organisation rules are written once.

Everything runs in the CALLER's transaction. In order:
  1. lock the organisation's `seat_state` row (the same row `membership_service.end` locks), so two joins cannot both
     take the last seat; an organisation without one is locked through its `organisations` row;
  2. refuse a user who already has an active membership (ALREADY_IN_ORG), then apply `seat_rules.can_join`;
  3. refuse a deleted user;
  4. insert the membership (the unique index `ux_memberships_one_active_org` is the final guard against a race);
  5. count the invite's use, if one was used;
  6. audit `member.joined`;
  7. enqueue `org.member.joined` (with org_type, keyed by user_id).
Seats: `seat_state.status` 'incomplete' or 'ended' means no seats are available (seats_bought NULL would otherwise read as
"no cap"). The FIRST owner of an organisation is the one exception: seats cannot be bought without an owner.
"""

import uuid
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import InviteKind, JoinDecision, JoinSource, MembershipStatus, OrgStatus, OrgType, Role, SeatStatus
from app.core.exceptions import AlreadyInOrg, ForbiddenRole, NotFound, join_decision_error
from app.db.errors import map_integrity_error
from app.events import topics
from app.events.producers import enqueue
from app.events.schemas import OrgMemberJoined
from app.models.invite import Invite
from app.models.membership import Membership
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import audit_service, seat_rules
from app.services.audit_service import Actor
from app.utils.ids import uuid7

NO_SEATS = frozenset({SeatStatus.INCOMPLETE.value, SeatStatus.ENDED.value})


async def join(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    source: JoinSource,
    role: Role = Role.MEMBER,
    invite: Invite | None = None,
    *,
    actor: Actor,
    staff: bool = False,
    clock: Clock = system_clock,
) -> Membership:
    members = MembershipRepository(session)

    # 1. serialise every join (and leave) of this organisation
    seat_state = await SeatStateRepository(session).lock_for_update(org_id)
    orgs = OrgRepository(session)
    org = await (orgs.get(org_id) if seat_state is not None else orgs.get_for_update(org_id))
    if org is None:
        raise NotFound("Organisation not found")
    if invite is not None and invite.org_id != org_id:
        raise ValueError("the invite belongs to another organisation")

    # 2. one active organisation per user, then the seat rules
    if await members.get_active_for_user(user_id) is not None:
        raise AlreadyInOrg("The user is already in an organisation")
    active_count = await members.count_active(org_id)
    active_owners = await members.count_active_owners(org_id)
    if role is Role.OWNER and active_owners > 0 and not staff:
        raise ForbiddenRole("an organisation with an owner is given a new owner by transfer or by staff")
    no_seats = seat_state is None or seat_state.status in NO_SEATS
    decision = seat_rules.can_join(
        OrgStatus(org.status), 0 if no_seats else seat_state.seats_bought, active_count,
        is_pilot=org.status == OrgStatus.PILOT.value, pilot_max_members=settings.PILOT_MAX_MEMBERS)
    first_owner = role is Role.OWNER and active_owners == 0
    if decision is JoinDecision.SEATS_FULL and first_owner and no_seats:
        decision = JoinDecision.OK     # seats are bought after the owner exists
    error = join_decision_error(decision)
    if error is not None:
        raise error

    # 3. a deleted user can never join
    known = await KnownUserRepository(session).get(user_id)
    if known is not None and known.deleted_at is not None:
        raise NotFound("User not found")

    # 4. the membership
    now: datetime = clock.now()
    membership = Membership(
        id=uuid7(), org_id=org_id, user_id=user_id, role=role.value, status=MembershipStatus.ACTIVE.value,
        source=source.value, invited_by=invite.created_by if invite is not None else None, joined_at=now)
    try:
        await members.add(membership)
    except IntegrityError as exc:           # a race the pre-check could not see: the unique index decides
        mapped = map_integrity_error(exc)
        if mapped is None:
            raise
        raise mapped from exc

    # 5. the invite
    if invite is not None:
        invite.uses += 1
        if invite.kind in (InviteKind.EMAIL.value, InviteKind.CSV.value):
            invite.accepted_at, invite.accepted_by = now, user_id
        await session.flush()

    # 6. audit
    await audit_service.record(
        session, actor, constants.AUDIT_MEMBER_JOINED, "membership", membership.id,
        {"user_id": str(user_id), "source": source.value, "role": role.value,
         "invite_id": str(invite.id) if invite is not None else None},
        org_id=org_id, staff=staff, clock=clock)

    # 7. org.member.joined
    event = OrgMemberJoined.create(clock, org_id=str(org_id), user_id=str(user_id), role=role,
                                   org_type=OrgType(org.type), source=source)
    await enqueue(session, topics.ORG_MEMBER_JOINED, str(user_id), event, clock)
    return membership
