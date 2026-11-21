"""Request and response bodies of the staff routes (/admin/org/v1)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Reason = Annotated[str, Field(min_length=5, max_length=500, description="Why staff are doing this: kept in the audit log")]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateOrgIn(_Request):
    name: str = Field(min_length=1, max_length=200)
    type: str
    legal_name: str | None = Field(default=None, max_length=300)
    gstin: str | None = None
    state_code: str | None = None
    billing_email: str
    owner_email: str
    pilot: bool
    reason: Reason


class AddMemberIn(_Request):
    user_id: uuid.UUID
    role: str = "member"
    reason: Reason


class SetSeatsIn(_Request):
    plan_code: str
    variant: str
    seats_bought: int | None = None
    period_end: datetime | None = None
    reason: Reason


class ReasonIn(_Request):
    reason: Reason


class ExtendPilotIn(_Request):
    days: int = Field(ge=1, le=30)
    reason: Reason


class SeatStateOut(BaseModel):
    plan_code: str
    variant: str
    seats_bought: int | None
    status: str
    period_end: datetime | None
    source: str
    refreshed_at: datetime


class OrgAdminOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    type: str
    legal_name: str | None
    gstin: str | None
    state_code: str | None
    billing_email: str | None
    status: str
    pilot_ends_at: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class CreatedOrgOut(OrgAdminOut):
    owner_joined: bool
    owner_invite_pending: bool


class OrgDetailOut(OrgAdminOut):
    seat_state: SeatStateOut | None
    active_count: int


class OrgPageOut(BaseModel):
    items: list[OrgAdminOut]
    next_cursor: str | None


class MembershipAdminOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    status: str
    source: str
    joined_at: datetime
