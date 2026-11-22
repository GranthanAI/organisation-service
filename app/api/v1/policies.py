"""GET and PUT /org/v1/orgs/{org_id}/policies: the organisation's policy, versioned with an ETag ("v<version>"). PUT is a full
replace and needs If-Match (428 without it, 412 VERSION_CONFLICT with the current version if it is stale)."""

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_clock, get_db_session, get_session_factory_dep
from app.api.idempotent_route import IdempotentRoute
from app.core.clock import Clock
from app.core.exceptions import PreconditionRequired
from app.db.session import unit_of_work
from app.schemas.org_v1 import PolicyIn, PolicyOut
from app.security.dependencies import require_org_role
from app.security.models import OrgContext
from app.services import policy_service
from app.services.permission_rules import Action

router = APIRouter(prefix="/orgs/{org_id}", tags=["Policies"], route_class=IdempotentRoute)


def out(view: policy_service.PolicyView) -> PolicyOut:
    return PolicyOut(version=view.version, lock_modes=view.lock_modes, approved_mode_ids=view.approved_mode_ids,
                     member_spend_cap_paise=view.member_spend_cap_paise, updated_at=view.updated_at, updated_by=view.updated_by)


@router.get("/policies", response_model=PolicyOut)
async def get_policy(response: Response, ctx: OrgContext = Depends(require_org_role(Action.VIEW_ORG)),
                     session: AsyncSession = Depends(get_db_session)):
    view = await policy_service.get_policy(session, ctx.org_id)
    response.headers["ETag"] = f'"v{view.version}"'
    return out(view)


@router.put("/policies", response_model=PolicyOut)
async def put_policy(body: PolicyIn, response: Response, if_match: str | None = Header(default=None),
                     ctx: OrgContext = Depends(require_org_role(Action.EDIT_POLICIES)),
                     factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    expected = policy_service.parse_if_match(if_match)
    if expected is None:
        raise PreconditionRequired('send If-Match: "v<version>" (the ETag of GET /policies)')
    async with unit_of_work(factory) as session:
        view = await policy_service.put_policy(session, ctx, expected, body.lock_modes, body.approved_mode_ids,
                                               body.member_spend_cap_paise, clock)
    response.headers["ETag"] = f'"v{view.version}"'
    return out(view)
