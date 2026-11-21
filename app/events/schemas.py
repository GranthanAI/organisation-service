"""Event models.

PRODUCED events are flat JSON objects: `event_id` (uuid7 string), `schema_version` "1.0", `occurred_at` (ISO UTC with
a Z), then the fields. They forbid unknown fields, so a typo never reaches the broker.

CONSUMED events ignore unknown fields (the producers may add some). Shapes are copied from auth-service:
  user-events:   {event_type, payload, created_at}; payload for UserRegistered is {event_id, timestamp, user_id,
                 email, full_name}, for EmailVerified {event_id, timestamp, user_id, email}
  user.deleted:  {event_type: "user.deleted", schema_version, event_id, occurred_at, user_id}
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.clock import Clock, system_clock
from app.core.enums import EndReason, JoinSource, OrgType, Role
from app.events import topics
from app.utils.ids import uuid7

SCHEMA_VERSION = "1.0"
_OCCURRED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def iso_z(moment: datetime) -> str:
    """UTC, whole seconds, trailing Z: 2026-11-21T12:00:00Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- produced
class ProducedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid7()))
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    occurred_at: str

    @field_validator("event_id")
    @classmethod
    def _event_id_is_a_uuid(cls, value: str) -> str:
        uuid.UUID(value)
        return value

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_iso_utc_with_z(cls, value: str) -> str:
        if not _OCCURRED_AT.match(value):
            raise ValueError("occurred_at must be ISO UTC ending in Z")
        return value

    @classmethod
    def create(cls, clock: Clock = system_clock, **fields: Any):
        return cls(occurred_at=iso_z(clock.now()), **fields)


class OrgMemberJoined(ProducedEvent):
    org_id: str
    user_id: str
    role: Role
    org_type: OrgType        # always sent: billing defaults a missing value to `company`
    source: JoinSource


class OrgMemberLeft(ProducedEvent):
    org_id: str
    user_id: str
    role: Role
    org_type: OrgType
    reason: EndReason


class OrgPolicyChanged(ProducedEvent):
    org_id: str
    version: int
    lock_modes: bool
    approved_mode_ids: list[str]
    member_spend_cap_paise: int | None


class OrgInviteCreated(ProducedEvent):
    org_id: str
    invite_id: str
    email: str
    org_name: str
    inviter_name: str | None
    accept_url: str
    expires_at: str


class OrgPilotEnding(ProducedEvent):
    org_id: str
    ends_at: str
    days_left: int


# topic -> (model, the field the message key must equal)
PRODUCED: dict[str, tuple[type[ProducedEvent], str]] = {
    topics.ORG_MEMBER_JOINED: (OrgMemberJoined, "user_id"),   # keyed by user_id: one person's join and leave stay in order
    topics.ORG_MEMBER_LEFT: (OrgMemberLeft, "user_id"),
    topics.ORG_POLICY_CHANGED: (OrgPolicyChanged, "org_id"),
    topics.ORG_INVITE_CREATED: (OrgInviteCreated, "org_id"),
    topics.ORG_PILOT_ENDING: (OrgPilotEnding, "org_id"),
}


# ---------------------------------------------------------------- consumed
class _Consumed(BaseModel):
    model_config = ConfigDict(extra="ignore")


class UserEventEnvelope(_Consumed):
    """A message on user-events. Only UserRegistered and EmailVerified are handled; others are skipped."""

    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class UserRegisteredPayload(_Consumed):
    event_id: str
    timestamp: datetime
    user_id: uuid.UUID
    email: str
    full_name: str | None = None


class EmailVerifiedPayload(_Consumed):
    event_id: str
    timestamp: datetime
    user_id: uuid.UUID
    email: str


class UserDeletedEvent(_Consumed):
    event_type: Literal["user.deleted"]
    schema_version: str
    event_id: str
    occurred_at: str
    user_id: uuid.UUID
