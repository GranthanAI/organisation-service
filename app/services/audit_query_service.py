"""Reading the audit log (Phase 6). Newest first, keyset on (at, id).

The organisation's owners and admins see what its people did; GranthanAI staff reasons are internal, so for the
organisation view a `reason` is shown only on rows written by a user, and `details` keys starting with "staff_" are
dropped. The staff view shows everything.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationFailed
from app.repositories.audit_repository import AuditRepository
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


@dataclass(frozen=True)
class AuditPage:
    items: list[AuditRow]
    next_cursor: str | None


def present(entry, *, staff_view: bool) -> AuditRow:
    details = dict(entry.details or {})
    reason = entry.reason
    if not staff_view:
        details = {k: v for k, v in details.items() if not str(k).startswith("staff_")}
        if entry.actor_kind != "user":
            reason = None
    return AuditRow(entry.id, entry.action, entry.actor_id, entry.actor_kind, entry.target_type, entry.target_id,
                    details, reason, entry.at)


async def list_audit(session: AsyncSession, org_id: uuid.UUID, cursor: str | None = None, limit: int = DEFAULT_LIMIT,
                     action: str | None = None, actor_id: uuid.UUID | None = None, *, staff_view: bool = False) -> AuditPage:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}")
    after = decode_cursor(cursor) if cursor else None
    rows = await AuditRepository(session).list_page(org_id, after=after, limit=limit + 1, action=action,
                                                    actor_id=actor_id)
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].at, page[-1].id) if len(rows) > limit else None
    return AuditPage([present(r, staff_view=staff_view) for r in page], next_cursor)
