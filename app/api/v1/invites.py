"""Email invites: create, manage (list, resend, revoke), preview (public) and accept.

The preview is the ONLY route here without a JWT (and without the idempotency route class): it is mounted on its own
router, rate-limited per IP. api-gateway must treat exactly GET /org/v1/invites/preview as public.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_clock, get_db_session, get_session_factory_dep
from app.api.idempotent_route import IdempotentRoute
from app.core.clock import Clock
from app.core.constants import RATE_INVITE_ACCEPT, RATE_INVITE_CREATE, RATE_INVITE_PREVIEW
from app.db.session import unit_of_work
from app.middleware.rate_limit import rate_limit
from app.schemas.org_v1 import (
    AcceptIn,
    AcceptOut,
    CreateInvitesIn,
    CreateInvitesOut,
    InvitePageOut,
    InviteResultOut,
    PreviewOut,
    SeatsInfoOut,
)
from app.security.dependencies import get_current_user, require_org_role
from app.security.models import CurrentUser, OrgContext
from app.services import invite_service
from app.services.permission_rules import Action
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT

# ---- public: no JWT, no idempotency
public_router = APIRouter(prefix="/invites", tags=["Invites (public)"])


@public_router.get("/preview", response_model=PreviewOut, dependencies=[Depends(rate_limit(RATE_INVITE_PREVIEW))])
async def preview(token: str = Query(min_length=8, max_length=200), session: AsyncSession = Depends(get_db_session),
                  clock: Clock = Depends(get_clock)):
    return await invite_service.preview(session, token, clock)


# ---- logged in
router = APIRouter(tags=["Invites"], route_class=IdempotentRoute)


@router.post("/orgs/{org_id}/invites", response_model=CreateInvitesOut, dependencies=[Depends(rate_limit(RATE_INVITE_CREATE))])
async def create_invites(body: CreateInvitesIn, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                         factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        results, seats = await invite_service.create_invites(session, ctx, body.emails, body.role, clock)
    return CreateInvitesOut(
        results=[InviteResultOut(email=r.email, result=r.result, invite_id=r.invite_id) for r in results],
        seats=SeatsInfoOut(used=seats.used, bought=seats.bought, open_invites=seats.open_invites, warning=seats.warning))


@router.get("/orgs/{org_id}/invites", response_model=InvitePageOut)
async def list_invites(status: str | None = None, cursor: str | None = None,
                       limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                       ctx: OrgContext = Depends(require_org_role(Action.VIEW_INVITES)),
                       session: AsyncSession = Depends(get_db_session), clock: Clock = Depends(get_clock)):
    return await invite_service.list_invites(session, ctx.org_id, status, cursor, limit, clock)


@router.post("/orgs/{org_id}/invites/{invite_id}/resend", response_model=InviteResultOut)
async def resend_invite(invite_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        invite = await invite_service.resend(session, ctx.org_id, invite_id, ctx.user, clock=clock)
    return InviteResultOut(email=str(invite.email), result="resent", invite_id=invite.id)


@router.delete("/orgs/{org_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(invite_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        await invite_service.revoke(session, ctx.org_id, invite_id, ctx.user, clock)
    return Response(status_code=204)


@router.post("/invites/accept", response_model=AcceptOut, dependencies=[Depends(rate_limit(RATE_INVITE_ACCEPT))])
async def accept_invite(body: AcceptIn, user: CurrentUser = Depends(get_current_user),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        return await invite_service.accept(session, user, body.token, clock)
