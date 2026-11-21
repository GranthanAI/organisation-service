"""Staff routes for organisations, at /admin/org/v1/orgs (GranthanAI staff only).

Writes need staff role `admin` and an Idempotency-Key (the route class); reads also allow `support`. Every write body has a
`reason` (5-500 characters), which goes to the audit log. Business rules live in services/org_service.py.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_clock, get_db_session, get_session_factory_dep
from app.api.idempotent_route import IdempotentRoute
from app.core.clock import Clock
from app.core.enums import StaffRole
from app.db.session import unit_of_work
from app.schemas.admin import (
    AddMemberIn,
    CreatedOrgOut,
    CreateOrgIn,
    ExtendPilotIn,
    MembershipAdminOut,
    OrgAdminOut,
    OrgDetailOut,
    OrgPageOut,
    ReasonIn,
    SeatStateOut,
    SetSeatsIn,
)
from app.schemas.org_v1 import AuditPageOut, InviteResultOut, MemberPageOut
from app.security.dependencies import require_staff
from app.security.models import CurrentUser
from app.services import audit_query_service, invite_service, org_member_service, org_service, seat_service
from app.services.permission_rules import Action
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/orgs", tags=["Admin: organisations"], route_class=IdempotentRoute)

Writer = Depends(require_staff(StaffRole.ADMIN))
Reader = Depends(require_staff(StaffRole.SUPPORT))


def org_out(org) -> OrgAdminOut:
    return OrgAdminOut.model_validate(org, from_attributes=True)


# ---------------------------------------------------------------- writes
@router.post("", status_code=201, response_model=CreatedOrgOut)
async def create_org(body: CreateOrgIn, actor: CurrentUser = Writer, factory: async_sessionmaker = Depends(get_session_factory_dep),
                     clock: Clock = Depends(get_clock)) -> CreatedOrgOut:
    async with unit_of_work(factory) as session:
        created = await org_service.create_org(session, actor, clock=clock, **body.model_dump())
    return CreatedOrgOut(**org_out(created.org).model_dump(), owner_joined=created.owner_joined,
                         owner_invite_pending=created.owner_invite_pending)


@router.post("/{org_id}/members", status_code=201, response_model=MembershipAdminOut)
async def add_member(org_id: uuid.UUID, body: AddMemberIn, actor: CurrentUser = Writer,
                     factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        membership = await org_service.add_member(session, actor, org_id, body.user_id, body.role, body.reason, clock)
    return MembershipAdminOut.model_validate(membership, from_attributes=True)


@router.post("/{org_id}/seats", response_model=SeatStateOut)
async def set_seats(org_id: uuid.UUID, body: SetSeatsIn, actor: CurrentUser = Writer,
                    factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        org_service.require_staff_action(Action.STAFF_SET_SEATS, actor)
        seat = await seat_service.set_from_staff(session, org_id, body.plan_code, body.variant, body.seats_bought,
                                                 body.period_end, actor, body.reason, clock=clock)
    return SeatStateOut.model_validate(seat, from_attributes=True)


@router.post("/{org_id}/suspend", response_model=OrgAdminOut)
async def suspend(org_id: uuid.UUID, body: ReasonIn, actor: CurrentUser = Writer,
                  factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        return org_out(await org_service.suspend(session, actor, org_id, body.reason, clock))


@router.post("/{org_id}/reactivate", response_model=OrgAdminOut)
async def reactivate(org_id: uuid.UUID, body: ReasonIn, actor: CurrentUser = Writer,
                     factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        return org_out(await org_service.reactivate(session, actor, org_id, body.reason, clock))


@router.post("/{org_id}/extend-pilot", response_model=OrgAdminOut)
async def extend_pilot(org_id: uuid.UUID, body: ExtendPilotIn, actor: CurrentUser = Writer,
                       factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        return org_out(await org_service.extend_pilot(session, actor, org_id, body.days, body.reason, clock))


@router.post("/{org_id}/close", response_model=OrgAdminOut)
async def close(org_id: uuid.UUID, body: ReasonIn, actor: CurrentUser = Writer,
                factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        return org_out(await org_service.close(session, actor, org_id, body.reason, clock))


# ---------------------------------------------------------------- reads (support may read)
@router.get("", response_model=OrgPageOut)
async def search_orgs(q: str | None = Query(default=None, max_length=100), status: str | None = None,
                      cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100),
                      _actor: CurrentUser = Reader, session=Depends(get_db_session)) -> OrgPageOut:
    page, next_cursor = await org_service.search_orgs(session, q, status, cursor, limit)
    return OrgPageOut(items=[org_out(o) for o in page], next_cursor=next_cursor)


@router.get("/{org_id}", response_model=OrgDetailOut)
async def get_org(org_id: uuid.UUID, _actor: CurrentUser = Reader, session=Depends(get_db_session)) -> OrgDetailOut:
    org, seat, active = await org_service.org_detail(session, org_id)
    return OrgDetailOut(**org_out(org).model_dump(),
                        seat_state=SeatStateOut.model_validate(seat, from_attributes=True) if seat else None,
                        active_count=active)


@router.get("/{org_id}/members", response_model=MemberPageOut)
async def staff_list_members(
    org_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    q: str | None = Query(default=None, min_length=2, max_length=100),
    role: str | None = None,
    _actor: CurrentUser = Reader,
    session=Depends(get_db_session),
):
    """The same list and pagination as /org/v1/orgs/{id}/members, for staff (support may read)."""
    await org_service.org_detail(session, org_id)             # 404 for an unknown organisation
    return await org_member_service.list_members(session, org_id, cursor, limit, q, role)


@router.get("/{org_id}/audit", response_model=AuditPageOut)
async def staff_audit(
    org_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    action: str | None = Query(default=None, max_length=100),
    actor_id: uuid.UUID | None = None,
    _actor: CurrentUser = Reader,
    session=Depends(get_db_session),
):
    """The whole audit log, staff reasons and staff_ details included."""
    await org_service.org_detail(session, org_id)
    return await audit_query_service.list_audit(session, org_id, cursor, limit, action, actor_id, staff_view=True)


@router.post("/{org_id}/invites/{invite_id}/resend", response_model=InviteResultOut)
async def staff_resend_invite(org_id: uuid.UUID, invite_id: uuid.UUID, body: ReasonIn, actor: CurrentUser = Writer,
                              factory: async_sessionmaker = Depends(get_session_factory_dep),
                              clock: Clock = Depends(get_clock)):
    """Resend an invite (typically the owner invite of a new organisation) with a new link. Needs a reason."""
    org_service.require_staff_action(Action.INVITE, actor)
    async with unit_of_work(factory) as session:
        invite = await invite_service.resend(session, org_id, invite_id, actor, staff=True, reason=body.reason, clock=clock)
    return InviteResultOut(email=str(invite.email), result="resent", invite_id=invite.id)
