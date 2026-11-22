"""Join requests (Phase 9): a user whose email domain belongs to a verified domain of an organisation asks to join; an admin
approves or declines. Also the auto-join on EmailVerified, which falls back to a request when the org is full.

Everything runs in the caller's transaction. Approving goes through the join transaction (seat rules apply; a refusal
leaves the request pending and the error passes through). A pending request is closed (declined, no decider) when the
user joins an organisation another way: join_service.join does that.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import JoinSource, Role
from app.core.exceptions import (
    AlreadyInOrg,
    DomainError,
    JoinRequestPending,
    NotFound,
    PilotFull,
    SeatsFull,
    ValidationFailed,
)
from app.core.logging import logger
from app.db.errors import map_integrity_error
from app.models.domain import JoinRequest
from app.repositories.domain_repository import DomainRepository
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.security.models import CurrentUser, OrgContext
from app.services import audit_service, domain_rules, join_service
from app.services.invite_service import ACCEPTING
from app.utils.ids import uuid7
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor


@dataclass(frozen=True)
class DomainMatch:
    org_id: uuid.UUID
    org_name: str
    org_type: str
    domain: str
    auto_join: bool
    pending_request_id: uuid.UUID | None


async def find_match(session: AsyncSession, user_id: uuid.UUID) -> DomainMatch | None:
    """The organisation this user's VERIFIED email belongs to (a verified domain of a pilot or active org), if they are
    not in an organisation already. One function for the match endpoint, join requests and auto-join."""
    known = await KnownUserRepository(session).get(user_id)
    if known is None or known.deleted_at is not None or known.email_verified_at is None:
        return None
    if await MembershipRepository(session).get_active_for_user(user_id) is not None:
        return None
    owner = await DomainRepository(session).find_verified_owner(domain_rules.email_domain(str(known.email)))
    if owner is None:
        return None
    org_id, domain, auto_join = owner
    org = await OrgRepository(session).get(org_id)
    if org is None or org.status not in ACCEPTING:
        return None
    pending = await DomainRepository(session).pending_request_for_user(user_id, org_id)
    return DomainMatch(org_id, org.name, org.type, domain, auto_join, pending.id if pending else None)


# ---------------------------------------------------------------------------------------------------- the user's side
async def create_request(session: AsyncSession, user: CurrentUser, clock: Clock = system_clock) -> JoinRequest:
    if await MembershipRepository(session).get_active_for_user(user.id) is not None:
        raise AlreadyInOrg("You are already in an organisation")
    match = await find_match(session, user.id)
    if match is None:
        raise NotFound("No organisation matches your email address")
    if match.pending_request_id is not None:
        raise JoinRequestPending("You already asked to join this organisation")
    return await _insert_request(session, user, user.id, match.org_id, match.domain, clock)


async def cancel_request(session: AsyncSession, user: CurrentUser, request_id: uuid.UUID, clock: Clock = system_clock) -> None:
    """The requester withdraws a pending request: it becomes declined, decided by themselves. Closed requests: no-op."""
    repo = DomainRepository(session)
    request = await repo.get_join_request_for_update(request_id)
    if request is None or request.user_id != user.id:
        raise NotFound("Join request not found")
    if request.status != "pending":
        return
    request.status, request.decided_by, request.decided_at = "declined", user.id, clock.now()
    await session.flush()
    await audit_service.record(session, user, constants.AUDIT_JOIN_REQUEST_DECLINED, "join_request", request.id,
                               {"user_id": str(user.id), "cancelled_by_requester": True}, org_id=request.org_id, clock=clock)


# ---------------------------------------------------------------------------------------------------- the admin's side
@dataclass(frozen=True)
class RequestRow:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str | None
    email: str | None
    domain: str
    status: str
    created_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None


@dataclass(frozen=True)
class RequestPage:
    items: list[RequestRow]
    next_cursor: str | None


async def list_requests(session: AsyncSession, org_id: uuid.UUID, status: str | None = "pending", cursor: str | None = None,
                        limit: int = DEFAULT_LIMIT) -> RequestPage:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}")
    if status is not None and status not in ("pending", "approved", "declined", "expired"):
        raise ValidationFailed("status must be pending, approved, declined or expired")
    after = decode_cursor(cursor) if cursor else None
    rows = await DomainRepository(session).list_join_requests_page(org_id, status, after, limit + 1)
    page = rows[:limit]
    people = await KnownUserRepository(session).get_many([r.user_id for r in page])
    items = [RequestRow(r.id, r.user_id, people[r.user_id].full_name if r.user_id in people else None,
                        str(people[r.user_id].email) if r.user_id in people else None, r.domain, r.status, r.created_at,
                        r.decided_by, r.decided_at) for r in page]
    return RequestPage(items, encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None)


async def _pending_of_org(session: AsyncSession, ctx: OrgContext, request_id: uuid.UUID) -> JoinRequest:
    request = await DomainRepository(session).get_join_request_for_update(request_id)
    if request is None or request.org_id != ctx.org_id:
        raise NotFound("Join request not found")
    if request.status != "pending":
        raise ValidationFailed("this request is no longer pending")
    return request


async def approve(session: AsyncSession, ctx: OrgContext, request_id: uuid.UUID, clock: Clock = system_clock) -> JoinRequest:
    request = await _pending_of_org(session, ctx, request_id)
    # the join transaction: seat rules apply, and a refusal (SEATS_FULL, PILOT_FULL, ALREADY_IN_ORG...) leaves the request pending
    await join_service.join(session, ctx.org_id, request.user_id, JoinSource.DOMAIN, Role.MEMBER, actor=ctx.user, clock=clock)
    request.status, request.decided_by, request.decided_at = "approved", ctx.user.id, clock.now()   # after join() closed the user's pending ones
    await session.flush()
    await audit_service.record(session, ctx.user, constants.AUDIT_JOIN_REQUEST_APPROVED, "join_request", request.id,
                               {"user_id": str(request.user_id), "domain": request.domain}, org_id=ctx.org_id, clock=clock)
    return request


async def decline(session: AsyncSession, ctx: OrgContext, request_id: uuid.UUID, clock: Clock = system_clock) -> JoinRequest:
    request = await _pending_of_org(session, ctx, request_id)
    request.status, request.decided_by, request.decided_at = "declined", ctx.user.id, clock.now()
    await session.flush()
    await audit_service.record(session, ctx.user, constants.AUDIT_JOIN_REQUEST_DECLINED, "join_request", request.id,
                               {"user_id": str(request.user_id), "domain": request.domain}, org_id=ctx.org_id, clock=clock)
    return request


# ---------------------------------------------------------------------------------------------------- auto-join on EmailVerified
async def auto_join_on_email_verified(session: AsyncSession, user_id: uuid.UUID, clock: Clock = system_clock) -> str:
    """Called by the EmailVerified handler, in its transaction (so it is deduplicated with the rest). Returns what happened:
    off | no_match | joined | requested | skipped. Never from UserRegistered; never retroactive (it looks at ONE user)."""
    if not settings.DOMAIN_AUTO_JOIN_ENABLED:
        return "off"
    match = await find_match(session, user_id)       # verified email, not deleted, no active org, org pilot/active
    if match is None or not match.auto_join:
        return "no_match"
    try:
        async with session.begin_nested():
            await join_service.join(session, match.org_id, user_id, JoinSource.DOMAIN, Role.MEMBER, actor="system", clock=clock)
        return "joined"
    except (SeatsFull, PilotFull):
        pass                                          # no room: ask the admins instead
    except DomainError as exc:
        logger.info("auto-join refused", code=exc.code)
        return "skipped"
    if match.pending_request_id is not None:
        return "requested"                            # already waiting: no duplicate
    try:
        async with session.begin_nested():
            await _insert_request(session, "system", user_id, match.org_id, match.domain, clock)
    except JoinRequestPending:
        pass
    return "requested"


async def _insert_request(session: AsyncSession, actor, user_id: uuid.UUID, org_id: uuid.UUID, domain: str,
                          clock: Clock) -> JoinRequest:
    request = JoinRequest(id=uuid7(), org_id=org_id, user_id=user_id, domain=domain, status="pending", created_at=clock.now())
    try:
        await DomainRepository(session).add_join_request(request)
    except IntegrityError as exc:
        mapped = map_integrity_error(exc)
        if mapped is None:
            raise
        raise mapped from exc
    await audit_service.record(session, actor, constants.AUDIT_JOIN_REQUEST_CREATED, "join_request", request.id,
                               {"user_id": str(user_id), "domain": domain}, org_id=org_id, clock=clock)
    return request
