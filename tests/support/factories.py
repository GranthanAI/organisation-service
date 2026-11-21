"""Builders for model rows used by the Postgres tests. Ids come from uuid7, times from a fixed instant."""

import uuid
from datetime import datetime, timedelta, timezone

from app.core.enums import (
    InviteKind,
    JoinSource,
    MembershipStatus,
    OrgStatus,
    OrgType,
    Role,
)
from app.models.audit_log import AuditLog
from app.models.invite import Invite
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.outbox import OutboxEvent
from app.models.seat_state import SeatState
from app.utils.ids import uuid7

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def make_org(**kw) -> Organisation:
    org_id = kw.pop("id", None) or uuid7()
    defaults = dict(
        id=org_id, name="Amity University", slug=f"amity-{uuid.uuid4().hex[:12]}", type=OrgType.COLLEGE.value,
        status=OrgStatus.ACTIVE.value, created_at=NOW, updated_at=NOW,
    )
    return Organisation(**{**defaults, **kw})


def make_membership(org_id: uuid.UUID, user_id: uuid.UUID | None = None, **kw) -> Membership:
    defaults = dict(
        id=uuid7(), org_id=org_id, user_id=user_id or uuid7(), role=Role.MEMBER.value,
        status=MembershipStatus.ACTIVE.value, source=JoinSource.STAFF.value, joined_at=NOW,
    )
    return Membership(**{**defaults, **kw})


def make_seat_state(org_id: uuid.UUID, **kw) -> SeatState:
    defaults = dict(org_id=org_id, plan_code="org_pro", variant="college", seats_bought=50, status="active",
                    source="staff", refreshed_at=NOW)
    return SeatState(**{**defaults, **kw})


def make_invite(org_id: uuid.UUID, **kw) -> Invite:
    defaults = dict(
        id=uuid7(), org_id=org_id, kind=InviteKind.EMAIL.value, email="student@amity.edu", role=Role.MEMBER.value,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex, expires_at=NOW + timedelta(days=14), created_at=NOW,
    )
    return Invite(**{**defaults, **kw})


def make_audit(org_id: uuid.UUID | None, **kw) -> AuditLog:
    defaults = dict(id=uuid7(), org_id=org_id, actor_kind="staff", action="org.created", at=NOW, details={"a": 1})
    return AuditLog(**{**defaults, **kw})


def make_outbox(**kw) -> OutboxEvent:
    defaults = dict(id=uuid7(), topic="org.member.joined", key=str(uuid7()), payload={"x": 1}, created_at=NOW,
                    next_attempt_at=NOW)
    return OutboxEvent(**{**defaults, **kw})
