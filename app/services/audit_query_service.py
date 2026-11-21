"""Reading the audit log (Phase 6). Newest first, keyset on (at, id).

The organisation's owners and admins see what its people did. GranthanAI's own actions are shown without who: staff rows
have `actor_id` null and the label "GranthanAI staff", service and system rows null and "GranthanAI"; user rows keep
`actor_id` and carry the person's name or email as `actor_label`. A `reason` is shown only on user rows and `details`
keys starting with "staff_" are dropped. The staff view shows everything.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationFailed
from app.repositories.audit_repository import AuditRepository
from app.repositories.known_user_repository import KnownUserRepository
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor


@dataclass(frozen=True)
class AuditRow:
    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    actor_kind: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    reason: str | None
    at: datetime
    actor_label: str | None = None


@dataclass(frozen=True)
class AuditPage:
    items: list[AuditRow]
    next_cursor: str | None


STAFF_LABEL = "GranthanAI staff"
SYSTEM_LABEL = "GranthanAI"
UNKNOWN_USER_LABEL = "A former member"


def present(entry, *, staff_view: bool, labels: dict[uuid.UUID, str] | None = None) -> AuditRow:
    """The organisation's view hides who at GranthanAI did something: staff rows show `actor_id` null and the label
    "GranthanAI staff", service and system rows show null and "GranthanAI", user rows keep `actor_id` and are labelled
    with the person's name or email. The staff view shows the real ids and no label."""
    details = dict(entry.details or {})
    reason, actor_id, label = entry.reason, entry.actor_id, None
    if not staff_view:
        details = {k: v for k, v in details.items() if not str(k).startswith("staff_") and k != "service"}
        if entry.actor_kind == "user":
            label = (labels or {}).get(entry.actor_id, UNKNOWN_USER_LABEL)
        else:
            reason, actor_id = None, None
            label = STAFF_LABEL if entry.actor_kind == "staff" else SYSTEM_LABEL
    return AuditRow(entry.id, entry.action, actor_id, entry.actor_kind, entry.target_type, entry.target_id,
                    details, reason, entry.at, label)


async def list_audit(session: AsyncSession, org_id: uuid.UUID, cursor: str | None = None, limit: int = DEFAULT_LIMIT,
                     action: str | None = None, actor_id: uuid.UUID | None = None, *, staff_view: bool = False) -> AuditPage:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}")
    after = decode_cursor(cursor) if cursor else None
    rows = await AuditRepository(session).list_page(org_id, after=after, limit=limit + 1, action=action,
                                                    actor_id=actor_id)
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].at, page[-1].id) if len(rows) > limit else None
    labels: dict[uuid.UUID, str] = {}
    if not staff_view:
        known = await KnownUserRepository(session).get_many({r.actor_id for r in page if r.actor_kind == "user" and r.actor_id})
        labels = {uid: (u.full_name or str(u.email)) for uid, u in known.items()}
    return AuditPage([present(r, staff_view=staff_view, labels=labels) for r in page], next_cursor)
