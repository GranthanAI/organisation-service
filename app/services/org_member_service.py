"""What an organisation's owners and admins do with its members (Phase 6): list, remove, leave.

Writes run in the caller's transaction, behind the status gate (`org_gate`), and audit as the acting user. The ending
itself is `membership_service.end`, which emits `org.member.left`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, system_clock
from app.core.enums import EndReason, Role
from app.core.exceptions import NotFound, ValidationFailed
from app.repositories.membership_repository import MembershipRepository
from app.security.models import OrgContext
from app.services import membership_service, org_gate
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor


@dataclass(frozen=True)
class MemberRow:
    user_id: uuid.UUID
    name: str | None
    email: str | None
    role: str
    source: str
    joined_at: datetime


@dataclass(frozen=True)
class MemberPage:
    items: list[MemberRow]
    next_cursor: str | None


async def list_members(session: AsyncSession, org_id: uuid.UUID, cursor: str | None = None, limit: int = DEFAULT_LIMIT,
                       q: str | None = None, role: str | None = None) -> MemberPage:
    """Active members, oldest first, keyset on (joined_at, id). `q` matches email or name (case-insensitive)."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}")
    if q is not None and not 2 <= len(q.strip()) <= 100:
        raise ValidationFailed("q must be 2 to 100 characters")
    if role is not None and role not in {r.value for r in Role}:
        raise ValidationFailed("role must be owner, admin or member")
    after = decode_cursor(cursor) if cursor else None
    rows = await MembershipRepository(session).list_page(
        org_id, after=after, limit=limit + 1, role=role, q=q.strip() if q else None)
    page = rows[:limit]
    items = [MemberRow(m.user_id, u.full_name if u else None, str(u.email) if u else None, m.role, m.source, m.joined_at)
             for m, u in page]
    next_cursor = encode_cursor(page[-1][0].joined_at, page[-1][0].id) if len(rows) > limit else None
    return MemberPage(items, next_cursor)


async def remove_member(session: AsyncSession, ctx: OrgContext, user_id: uuid.UUID,
                        clock: Clock = system_clock) -> None:
    """An owner removes anyone, an admin removes members only (`ctx.check_target`). Not yourself (use leave)."""
    await membership_service.lock_org(session, ctx.org_id)           # seat_state first, then the member's row
    await org_gate.require_open(session, ctx.org_id)
    if user_id == ctx.user.id:
        raise ValidationFailed("you cannot remove yourself: leave the organisation instead")
    ctx, target = await membership_service.lock_actor_and_target(session, ctx, user_id)
    if target is None:
        raise NotFound("Member not found")
    ctx.check_target(Role(target.role))                    # ctx now carries the caller role read under the lock
    await membership_service.end(session, ctx.org_id, user_id, EndReason.REMOVED, ctx.user, clock=clock)


async def leave(session: AsyncSession, ctx: OrgContext, clock: Clock = system_clock) -> None:
    """The caller leaves. The last owner gets LAST_OWNER (transfer ownership first)."""
    await membership_service.lock_org(session, ctx.org_id)
    await org_gate.require_open(session, ctx.org_id)
    await membership_service.end(session, ctx.org_id, ctx.user.id, EndReason.LEFT, ctx.user, clock=clock)
