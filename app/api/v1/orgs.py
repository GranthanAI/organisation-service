"""GET and PATCH /org/v1/orgs/{org_id}: the organisation's details (any member reads, owner or admin edits)."""


from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_clock, get_db_session, get_session_factory_dep
from app.api.idempotent_route import IdempotentRoute
from app.core.clock import Clock
from app.db.session import unit_of_work
from app.schemas.org_v1 import OrgPatchIn, OrgPublicOut
from app.security.dependencies import require_org_role
from app.security.models import OrgContext
from app.services import org_profile_service
from app.services.permission_rules import Action

router = APIRouter(prefix="/orgs/{org_id}", tags=["Organisation"], route_class=IdempotentRoute)


@router.get("", response_model=OrgPublicOut)
async def get_org(ctx: OrgContext = Depends(require_org_role(Action.VIEW_ORG)),
                  session: AsyncSession = Depends(get_db_session)):
    return await org_profile_service.get_details(session, ctx.org_id)


@router.patch("", response_model=OrgPublicOut)
async def patch_org(body: OrgPatchIn, ctx: OrgContext = Depends(require_org_role(Action.EDIT_DETAILS)),
                    factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        org = await org_profile_service.patch_details(session, ctx, body.model_dump(exclude_unset=True), clock)
    return org
