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
    actor_label: str | None = None   # org view only: a person's name or email, "GranthanAI staff" or "GranthanAI"


class AuditPageOut(BaseModel):
    items: list[AuditRowOut]
    next_cursor: str | None


# ---- invites
class CreateInvitesIn(_Request):
    emails: list[str] = Field(min_length=1, max_length=50)
    role: str = "member"


class InviteResultOut(BaseModel):
    email: str
    result: str     # created | invalid_email | duplicate_in_request | already_member | in_another_org | open_invite_exists
    invite_id: uuid.UUID | None = None


class SeatsInfoOut(BaseModel):
    used: int
    bought: int | None
    open_invites: int
    warning: bool


class CreateInvitesOut(BaseModel):
    results: list[InviteResultOut]
    seats: SeatsInfoOut


class PreviewOut(BaseModel):
    org_name: str
    org_type: str
    inviter_name: str
    role: str
    email_hint: str | None      # null for a link
    expires_at: datetime


class AcceptIn(_Request):
    token: str = Field(min_length=8, max_length=200)


class AcceptOut(BaseModel):
    org_id: uuid.UUID
    org_name: str
    role: str


class InviteRowOut(BaseModel):
    invite_id: uuid.UUID
    email: str | None
    role: str
    status: str                       # open | accepted | expired | revoked (computed)
    created_by: uuid.UUID | None
    created_at: datetime
    expires_at: datetime
    last_sent_at: datetime | None
    resend_count: int
    accepted_by: uuid.UUID | None
    accepted_at: datetime | None


class InvitePageOut(BaseModel):
    items: list[InviteRowOut]
    next_cursor: str | None


# ---- CSV bulk invites and invite batches (Phase 8)
class CsvErrorOut(BaseModel):
    row: int
    email: str
    reason: str


class CsvSeatsOut(BaseModel):
    used: int
    bought: int | None
    open_invites: int


class CsvUploadOut(BaseModel):
    batch_id: uuid.UUID
    dry_run: bool
    rows_total: int
    rows_ok: int
    rows_failed: int
    errors_preview: list[CsvErrorOut]
    seats: CsvSeatsOut


class BatchOut(BaseModel):
    batch_id: uuid.UUID
    status: str
    dry_run: bool
    rows_total: int
    rows_ok: int
    rows_failed: int
    sent: int
    pending_send: int
    not_sent: int
    errors_note: str | None = None      # why a stopped batch stopped: "org inactive" or "invites closed"
    created_at: datetime
    finished_at: datetime | None


# ---- invite links
class CreateLinkIn(_Request):
    max_uses: int = Field(ge=1, le=5000)
    expires_in_days: int = Field(ge=1)
    domain_restricted: bool = False


class LinkCreatedOut(BaseModel):
    link_id: uuid.UUID
    url: str
    max_uses: int
    expires_at: datetime
    domain_restricted: bool


class LinkOut(BaseModel):
    link_id: uuid.UUID
    created_by: uuid.UUID | None
    created_at: datetime
    expires_at: datetime
    max_uses: int | None
    uses: int
    domain_restricted: bool
    status: str      # open | used_up | expired | revoked


class LinkListOut(BaseModel):
    items: list[LinkOut]


class LinkUrlOut(BaseModel):
    link_id: uuid.UUID
    url: str
