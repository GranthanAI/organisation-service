"""GET /internal/orgs/{org_id} and GET /internal/orgs/{org_id}/members, for billing-service."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_internal_session
from app.schemas.internal import MembersOut, OrgOut
from app.services import directory_service

router = APIRouter()


@router.get("/orgs/{org_id}", response_model=OrgOut)
async def get_org(org_id: uuid.UUID, session: AsyncSession = Depends(get_internal_session)) -> dict:
    return await directory_service.org_details(session, org_id)


@router.get("/orgs/{org_id}/members", response_model=MembersOut)
async def get_members(org_id: uuid.UUID, session: AsyncSession = Depends(get_internal_session)) -> dict:
    return await directory_service.member_ids(session, org_id)
