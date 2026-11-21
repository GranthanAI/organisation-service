"""GET /org/v1/me: the caller's own organisation, role, seats and features (404 if they are in none)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.api.idempotent_route import IdempotentRoute
from app.api.internal.timing import time_request
from app.schemas.org_v1 import MeOut
from app.security.dependencies import get_current_user
from app.security.models import CurrentUser
from app.services import me_service

router = APIRouter(tags=["Organisation"], route_class=IdempotentRoute)


@router.get("/me", response_model=MeOut, dependencies=[Depends(time_request)])
async def me(user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)) -> dict:
    return await me_service.get_me(session, user.id)
