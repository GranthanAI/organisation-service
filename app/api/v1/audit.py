"""GET /org/v1/orgs/{org_id}/audit: what the organisation's admins did, newest first (owner or admin)."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.api.idempotent_route import IdempotentRoute
from app.schemas.org_v1 import AuditPageOut
from app.security.dependencies import require_org_role
from app.security.models import OrgContext
from app.services import audit_query_service
from app.services.permission_rules import Action
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/orgs/{org_id}", tags=["Audit"], route_class=IdempotentRoute)


@router.get("/audit", response_model=AuditPageOut)
async def audit_log(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    action: str | None = Query(default=None, max_length=100),
    actor_id: uuid.UUID | None = None,
    ctx: OrgContext = Depends(require_org_role(Action.VIEW_AUDIT)),
    session: AsyncSession = Depends(get_db_session),
):
    return await audit_query_service.list_audit(session, ctx.org_id, cursor, limit, action, actor_id, staff_view=False)
