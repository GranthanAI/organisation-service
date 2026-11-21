"""Request and response bodies of /org/v1 (what an organisation's own members and admins see)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---- /me
class MeOrg(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    type: str
    status: str
    pilot_ends_at: datetime | None


class MeSeats(BaseModel):
    plan_code: str | None
    status: str | None
    used: int
    bought: int | None        # null for pay as you go, and while seats are incomplete or ended
    over_by: int              # 0 if not over


class MeFeatures(BaseModel):
    lock_modes: bool
    shared_modes_max: int | None   # null = unlimited
    usage_reports: bool
    spend_cap: bool
    csv_invites: bool
    invite_links: bool
    domains: bool


class MeOut(BaseModel):
    org: MeOrg
    role: str
    seats: MeSeats
    features: MeFeatures


# ---- details
class OrgPublicOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    type: str
    status: str
    legal_name: str | None
    gstin: str | None
    state_code: str | None
    billing_email: str | None
    pilot_ends_at: datetime | None
    created_at: datetime


class OrgPatchIn(_Request):
    name: str | None = None
    legal_name: str | None = Field(default=None, max_length=300)
    gstin: str | None = None
    state_code: str | None = None
    billing_email: str | None = None


# ---- members
class MemberOut(BaseModel):
    user_id: uuid.UUID
    name: str | None
    email: str | None
    role: str
    source: str
    joined_at: datetime


class MemberPageOut(BaseModel):
    items: list[MemberOut]
    next_cursor: str | None


class ChangeRoleIn(_Request):
    role: str


class MemberRoleOut(BaseModel):
    user_id: uuid.UUID
    role: str


class TransferIn(_Request):
    to_user_id: uuid.UUID


class TransferOut(BaseModel):
    owner_id: uuid.UUID
    previous_owner_id: uuid.UUID
    previous_owner_role: str


class LeftOut(BaseModel):
    left: bool


# ---- audit
class AuditRowOut(BaseModel):
    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    actor_kind: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    reason: str | None
    at: datetime


class AuditPageOut(BaseModel):
    items: list[AuditRowOut]
    next_cursor: str | None
