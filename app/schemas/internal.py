"""Response bodies of the /internal routes billing-service calls (docs/PHASES.md Contracts 1)."""

import uuid

from pydantic import BaseModel


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    org_type: str          # college | company
    role: str
    status: str            # "active" only while the organisation is pilot or active; else the org status


class OrgOut(BaseModel):
    org_id: uuid.UUID
    name: str
    type: str
    gstin: str | None
    billing_email: str | None
    state_code: str | None
    member_spend_cap_paise: int | None


class MembersOut(BaseModel):
    user_ids: list[uuid.UUID]
