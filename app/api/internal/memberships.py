"""GET /internal/memberships/{user_id} and the alias /internal/users/{user_id}/membership (the path billing's
contracts/org-service.md still names): the user's active membership, or 404."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.schemas.internal import MembershipOut
from app.services import directory_service

router = APIRouter()


async def _membership(user_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)) -> dict:
    return await directory_service.membership_for_user(session, user_id)


router.add_api_route("/memberships/{user_id}", _membership, methods=["GET"], response_model=MembershipOut)
router.add_api_route("/users/{user_id}/membership", _membership, methods=["GET"], response_model=MembershipOut)
