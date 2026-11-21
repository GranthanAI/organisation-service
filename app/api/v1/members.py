"""/org/v1/orgs/{org_id}: the member list, role changes, removal, leaving and transferring ownership.

Every route uses `require_org_role(<action>)`: a non-member gets 404, a member without the right gets 403. Writes are
behind the organisation status gate (a closed organisation refuses them) and audited as the acting user.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_clock, get_db_session, get_session_factory_dep
from app.api.idempotent_route import IdempotentRoute
from app.core.clock import Clock
from app.core.enums import Role
from app.core.exceptions import ValidationFailed
from app.db.session import unit_of_work
from app.schemas.org_v1 import ChangeRoleIn, LeftOut, MemberPageOut, MemberRoleOut, TransferIn, TransferOut
from app.security.dependencies import require_org_role
from app.security.models import OrgContext
from app.services import membership_service, org_member_service
from app.services.permission_rules import Action
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/orgs/{org_id}", tags=["Members"], route_class=IdempotentRoute)


@router.get("/members", response_model=MemberPageOut)
async def list_members(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    q: str | None = Query(default=None, min_length=2, max_length=100),
    role: str | None = None,
    ctx: OrgContext = Depends(require_org_role(Action.VIEW_MEMBERS)),
    session: AsyncSession = Depends(get_db_session),
):
    return await org_member_service.list_members(session, ctx.org_id, cursor, limit, q, role)


@router.patch("/members/{user_id}", response_model=MemberRoleOut)
async def change_role(user_id: uuid.UUID, body: ChangeRoleIn,
                      ctx: OrgContext = Depends(require_org_role(Action.CHANGE_ROLE)),
                      factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    try:
        new_role = Role(body.role)
    except ValueError as exc:
        raise ValidationFailed("role must be admin or member") from exc
    async with unit_of_work(factory) as session:
        membership = await membership_service.change_role(session, ctx.org_id, user_id, new_role, ctx, clock=clock)
    return MemberRoleOut(user_id=membership.user_id, role=membership.role)


@router.delete("/members/{user_id}", status_code=204)
async def remove_member(user_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.REMOVE_MEMBER)),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        await org_member_service.remove_member(session, ctx, user_id, clock)
    return Response(status_code=204)


@router.post("/leave", response_model=LeftOut)
async def leave(ctx: OrgContext = Depends(require_org_role(Action.LEAVE)),
                factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        await org_member_service.leave(session, ctx, clock)
    return LeftOut(left=True)


@router.post("/transfer-ownership", response_model=TransferOut)
async def transfer_ownership(body: TransferIn, ctx: OrgContext = Depends(require_org_role(Action.TRANSFER_OWNERSHIP)),
                             factory: async_sessionmaker = Depends(get_session_factory_dep),
                             clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        new_owner, old_owner = await membership_service.transfer_ownership(session, ctx.org_id, body.to_user_id, ctx,
                                                                           clock=clock)
    return TransferOut(owner_id=new_owner.user_id, previous_owner_id=old_owner.user_id,
                       previous_owner_role=old_owner.role)
