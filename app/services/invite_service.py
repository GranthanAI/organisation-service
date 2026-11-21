"""Email invites (Phase 7): create, preview, accept, list, resend, revoke.

TOKEN DESIGN. The raw invite token is never stored: not in `invites`, the outbox, jobs, logs, Kafka or API responses (only
the console email in development shows a link). The token is DERIVED, `HMAC(INVITE_TOKEN_SECRET, "invite:<id>:<version>")`
(security/tokens.py); the row keeps its SHA-256 as `token_hash`, which is how a link is found. The `invite.send` job
recomputes the token when it sends. A resend bumps `token_version`, which gives a new token and kills the old link.

Every function runs in the CALLER's transaction. Sending the email happens afterwards, in the job queued here.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import InviteKind, InviteRevokeReason, JoinSource, OrgStatus, Role, SeatStatus
from app.core.exceptions import (
    ForbiddenRole,
    InvalidEmail,
    InviteEmailMismatch,
    InviteExpired,
    InviteInvalid,
    NotFound,
    OrgInactive,
    RateLimited,
    ValidationFailed,
)
from app.db.errors import map_integrity_error
from app.db.session import unit_of_work
from app.events import topics
from app.events.producers import enqueue
from app.events.schemas import OrgInviteCreated, iso_z
from app.models.invite import Invite
from app.models.organisation import Organisation
from app.repositories.invite_repository import InviteRepository
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser, OrgContext
from app.security.tokens import hash_token, invite_token_hash
from app.services import audit_service, job_service, join_service
from app.services.email_rules import normalize_email
from app.utils.ids import uuid7
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

CREATED, INVALID_EMAIL, DUPLICATE, ALREADY_MEMBER = "created", "invalid_email", "duplicate_in_request", "already_member"
IN_ANOTHER_ORG, OPEN_EXISTS = "in_another_org", "open_invite_exists"
ACCEPTING = frozenset({OrgStatus.PILOT.value, OrgStatus.ACTIVE.value})   # statuses that can invite and take members
NO_SEATS = {SeatStatus.INCOMPLETE.value, SeatStatus.ENDED.value}
ANONYMOUS_INVITER = "An organisation admin"
MAX_EMAILS = 50


# ---------------------------------------------------------------------------------------------------- helpers
def invite_status(invite: Invite, now: datetime) -> str:
    """open | accepted | expired | revoked, computed: an open invite past expires_at shows as expired."""
    if invite.accepted_at is not None:
        return "accepted"
    if invite.revoked_at is not None:
        return "expired" if invite.revoke_reason == InviteRevokeReason.EXPIRED.value else "revoked"
    return "expired" if invite.expires_at <= now else "open"


def email_hint(email: str | None) -> str:
    """s***@amity.edu: the first character of the local part, then ***, then the domain."""
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


async def display_name(session: AsyncSession, user_id: uuid.UUID | None, fallback: str | None = None) -> str | None:
    if user_id is None:
        return fallback
    known = await KnownUserRepository(session).get(user_id)
    if known is None:
        return fallback
    return known.full_name or str(known.email) or fallback


async def queue_send(session: AsyncSession, invite: Invite, now: datetime) -> bool:
    """Queue the `invite.send` job (in the caller's transaction, so the email goes out only if the invite commits).
    With INVITE_EMAILS_VIA=notification nothing is queued: the event alone is published."""
    if settings.INVITE_EMAILS_VIA != "local":
        return False
    return await job_service.schedule(
        session, constants.JOB_INVITE_SEND, now, {"invite_id": str(invite.id), "token_version": invite.token_version},
        dedupe_key=f"{constants.JOB_INVITE_SEND}:{invite.id}:{invite.token_version}")


async def announce(session: AsyncSession, org: Organisation, invite: Invite, inviter_name: str | None,
                   clock: Clock) -> None:
    """org.invite.created (no token, no URL) and the email job."""
    event = OrgInviteCreated.create(
        clock, org_id=str(org.id), invite_id=str(invite.id), email=invite.email, role=Role(invite.role),
        org_name=org.name, inviter_name=inviter_name, expires_at=iso_z(invite.expires_at))
    await enqueue(session, topics.ORG_INVITE_CREATED, str(org.id), event, clock)
    await queue_send(session, invite, clock.now())


def _seconds_to_midnight(now: datetime) -> int:
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    return int((midnight - now).total_seconds()) + 1


# ---------------------------------------------------------------------------------------------------- create
@dataclass(frozen=True)
class InviteResult:
    email: str
    result: str
    invite_id: uuid.UUID | None = None


@dataclass(frozen=True)
class SeatsInfo:
    used: int
    bought: int | None
    open_invites: int
    warning: bool     # used + open invites > bought: only informs, acceptance enforces the seats


async def seats_info(session: AsyncSession, org_id: uuid.UUID, now: datetime) -> SeatsInfo:
    seat = await SeatStateRepository(session).get(org_id)
    used = await MembershipRepository(session).count_active(org_id)
    open_invites = await InviteRepository(session).count_open(org_id, now)
    bought = None if seat is None or seat.status in NO_SEATS else seat.seats_bought
    return SeatsInfo(used, bought, open_invites, bought is not None and used + open_invites > bought)


async def create_invites(session: AsyncSession, ctx: OrgContext, emails: list[str], role: str,
                         clock: Clock = system_clock) -> tuple[list[InviteResult], SeatsInfo]:
    """One transaction for the whole request. Only the owner may invite an admin; only a pilot or active org may
    invite; at most INVITES_PER_ORG_PER_DAY invites per UTC day (429 over that). Per email, in order: invalid, duplicate
    in this request, already a member of this org, in another org (never says which), an expired open invite is closed,
    an open invite exists, otherwise created (job queued, event enqueued, audited)."""
    if not 1 <= len(emails) <= MAX_EMAILS:
        raise ValidationFailed(f"send between 1 and {MAX_EMAILS} emails")
    try:
        role_enum = Role(role)
    except ValueError as exc:
        raise ValidationFailed("role must be member or admin") from exc
    if role_enum not in (Role.MEMBER, Role.ADMIN):
        raise ValidationFailed("role must be member or admin")
    if role_enum is Role.ADMIN and ctx.role is not Role.OWNER:
        raise ForbiddenRole("only an owner can invite an admin")
    org = await OrgRepository(session).get(ctx.org_id)
    if org is None:
        raise NotFound("Organisation not found")
    if org.status not in ACCEPTING:
        raise OrgInactive("this organisation cannot invite right now")

    now = clock.now()
    invites, members, users = InviteRepository(session), MembershipRepository(session), KnownUserRepository(session)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    created_today = await invites.count_created_since(ctx.org_id, day_start)
    limit = settings.INVITES_PER_ORG_PER_DAY
    if created_today >= limit:
        raise RateLimited(_seconds_to_midnight(now))
    inviter = await display_name(session, ctx.user.id, ctx.user.email)

    results: list[InviteResult] = []
    seen: set[str] = set()
    created = 0
    for raw in emails:
        try:
            email = normalize_email(raw)
        except InvalidEmail:
            results.append(InviteResult(raw.strip()[:254], INVALID_EMAIL))
            continue
        if email in seen:
            results.append(InviteResult(email, DUPLICATE))
            continue
        seen.add(email)
        known = await users.get_by_email(email)
        existing = await members.get_active_for_user(known.user_id) if known is not None else None
        if existing is not None:
            results.append(InviteResult(email, ALREADY_MEMBER if existing.org_id == ctx.org_id else IN_ANOTHER_ORG))
            continue
        await invites.close_expired_for_email(ctx.org_id, email, now)       # the H4 rule, applied lazily
        if await invites.get_open_for_email(ctx.org_id, email) is not None:
            results.append(InviteResult(email, OPEN_EXISTS))
            continue
        if created_today + created >= limit:
            raise RateLimited(_seconds_to_midnight(now))
        invite_id = uuid7()
        invite = Invite(
            id=invite_id, org_id=ctx.org_id, kind=InviteKind.EMAIL.value, email=email, role=role_enum.value,
            token_hash=invite_token_hash(invite_id, 1), expires_at=now + timedelta(days=settings.INVITE_TTL_DAYS),
            created_by=ctx.user.id, created_at=now)
        try:
            async with session.begin_nested():     # a concurrent request may have just opened one: that is a result, not an error
                await invites.add(invite)
        except IntegrityError as exc:
            if map_integrity_error(exc) is None:
                raise
            results.append(InviteResult(email, OPEN_EXISTS))
            continue
        created += 1
        await announce(session, org, invite, inviter, clock)
        await audit_service.record(session, ctx.user, constants.AUDIT_INVITE_CREATED, "invite", invite.id,
                                   {"email": email, "role": role_enum.value}, org_id=ctx.org_id, clock=clock)
        results.append(InviteResult(email, CREATED, invite.id))
    return results, await seats_info(session, ctx.org_id, now)


# ---------------------------------------------------------------------------------------------------- preview
async def _find(session: AsyncSession, token: str) -> Invite | None:
    return await InviteRepository(session).get_by_token_hash(hash_token(token))


def _closed_error(invite: Invite, now: datetime) -> InviteInvalid | InviteExpired | None:
    """What an invite that can no longer be used answers (None: it can). Expired (even if the expiry job already closed
    it) is 410; revoked, accepted, purged or anything else is 404."""
    if invite.accepted_at is not None:
        return InviteInvalid("This invitation is not valid")
    if invite.revoked_at is not None:
        if invite.revoke_reason == InviteRevokeReason.EXPIRED.value:
            return InviteExpired("This invitation has expired")
        return InviteInvalid("This invitation is not valid")
    if invite.purged_at is not None:
        return InviteInvalid("This invitation is not valid")
    if invite.expires_at <= now:
        return InviteExpired("This invitation has expired")
    return None


async def preview(session: AsyncSession, token: str, clock: Clock = system_clock) -> dict:
    """What the join page shows before sign-in. No JWT. Anything unusable is 404 INVITE_INVALID, expired is 410, and an
    organisation that cannot take members is also 404 (anonymous callers learn nothing about an org's state)."""
    invite = await _find(session, token)
    if invite is None:
        raise InviteInvalid("This invitation is not valid")
    error = _closed_error(invite, clock.now())
    if error is not None:
        raise error
    org = await OrgRepository(session).get(invite.org_id)
    if org is None or org.status not in ACCEPTING:
        raise InviteInvalid("This invitation is not valid")
    inviter = await display_name(session, invite.created_by, None) or ANONYMOUS_INVITER
    return {"org_name": org.name, "org_type": org.type, "inviter_name": inviter, "role": invite.role,
            "email_hint": email_hint(invite.email), "expires_at": invite.expires_at}


# ---------------------------------------------------------------------------------------------------- accept
async def _close_expired_alone(session: AsyncSession, invite_id: uuid.UUID, now: datetime) -> None:
    """Close an expired invite in ITS OWN transaction, because the caller is about to raise (which rolls back its own)."""
    factory = async_sessionmaker(bind=session.bind, expire_on_commit=False, autoflush=False)
    async with unit_of_work(factory) as own:
        invite = await InviteRepository(own).get_for_update(invite_id)
        if invite is not None and invite.accepted_at is None and invite.revoked_at is None:
            invite.revoked_at, invite.revoke_reason = now, InviteRevokeReason.EXPIRED.value


async def accept(session: AsyncSession, user: CurrentUser, token: str, clock: Clock = system_clock) -> dict:
    """Join the organisation the invite belongs to, as the invite's role, in one transaction (join_service.join, so every
    refusal passes through and the invite stays open)."""
    now = clock.now()
    found = await _find(session, token)
    if found is None:
        raise InviteInvalid("This invitation is not valid")
    if found.accepted_at is None and found.revoked_at is None and found.expires_at <= now:
        await _close_expired_alone(session, found.id, now)
        raise InviteExpired("This invitation has expired")

    invite = await InviteRepository(session).get_for_update(found.id)     # two accepts of one invite queue here
    if invite is None or invite.token_hash != hash_token(token):
        raise InviteInvalid("This invitation is not valid")
    org = await OrgRepository(session).get(invite.org_id)
    body = {"org_id": invite.org_id, "org_name": org.name if org else "", "role": invite.role}

    if invite.accepted_at is not None:
        # a retry by the same person is safe; anyone else cannot use a spent invite
        mine = await MembershipRepository(session).get_active(invite.org_id, user.id)
        if invite.accepted_by == user.id and mine is not None:
            return body
        raise InviteInvalid("This invitation is not valid")
    error = _closed_error(invite, now)
    if error is not None:
        raise error
    if invite.kind in (InviteKind.EMAIL.value, InviteKind.CSV.value):
        if invite.email is None or normalize_email(str(invite.email)) != user.email:
            raise InviteEmailMismatch("This invitation was sent to a different email address")
    source = {InviteKind.EMAIL.value: JoinSource.INVITE, InviteKind.CSV.value: JoinSource.CSV,
              InviteKind.LINK.value: JoinSource.LINK}[invite.kind]
    await join_service.join(session, invite.org_id, user.id, source, Role(invite.role), invite, actor=user, clock=clock)
    return body


# ---------------------------------------------------------------------------------------------------- manage
@dataclass(frozen=True)
class InviteRow:
    invite_id: uuid.UUID
    email: str | None
    role: str
    status: str
    created_by: uuid.UUID | None
    created_at: datetime
    expires_at: datetime
    last_sent_at: datetime | None
    resend_count: int
    accepted_by: uuid.UUID | None
    accepted_at: datetime | None


@dataclass(frozen=True)
class InvitePage:
    items: list[InviteRow]
    next_cursor: str | None


async def list_invites(session: AsyncSession, org_id: uuid.UUID, status: str | None = None, cursor: str | None = None,
                       limit: int = DEFAULT_LIMIT, clock: Clock = system_clock) -> InvitePage:
    """Newest first, keyset on (created_at, id). The status is computed; the token hash is never returned."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}")
    if status is not None and status not in ("open", "accepted", "expired", "revoked"):
        raise ValidationFailed("status must be open, accepted, expired or revoked")
    now = clock.now()
    after = decode_cursor(cursor) if cursor else None
    rows = await InviteRepository(session).list_for_org(org_id, status, now, after, limit + 1)
    page = rows[:limit]
    items = [InviteRow(i.id, str(i.email) if i.email else None, i.role, invite_status(i, now), i.created_by, i.created_at,
                       i.expires_at, i.last_sent_at, i.resend_count, i.accepted_by, i.accepted_at) for i in page]
    return InvitePage(items, encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None)


async def resend(session: AsyncSession, org_id: uuid.UUID, invite_id: uuid.UUID, actor: CurrentUser, *,
                 staff: bool = False, reason: str | None = None, clock: Clock = system_clock) -> Invite:
    """A new token (the old link dies at once), a fresh expiry, and the email again. Open invites only (not accepted,
    not revoked: one that is merely past its expiry is revived); at most 3 resends."""
    invite = await InviteRepository(session).get_for_update(invite_id)
    if invite is None or invite.org_id != org_id:
        raise NotFound("Invitation not found")
    if invite.accepted_at is not None or invite.revoked_at is not None or invite.email is None:
        raise InviteInvalid("This invitation can no longer be resent")
    org = await OrgRepository(session).get(org_id)
    if org is None or org.status not in ACCEPTING:
        raise OrgInactive("this organisation cannot invite right now")
    if invite.resend_count >= constants.INVITE_RESEND_MAX:
        raise RateLimited(86400)
    now = clock.now()
    invite.token_version += 1
    invite.token_hash = invite_token_hash(invite.id, invite.token_version)
    invite.expires_at = now + timedelta(days=settings.INVITE_TTL_DAYS)
    invite.resend_count += 1
    await InviteRepository(session).save(invite)
    await queue_send(session, invite, now)
    await audit_service.record(session, actor, constants.AUDIT_INVITE_RESENT, "invite", invite.id,
                               {"email": str(invite.email), "resend_count": invite.resend_count}, reason,
                               org_id=org_id, staff=staff, clock=clock)
    return invite


async def revoke(session: AsyncSession, org_id: uuid.UUID, invite_id: uuid.UUID, actor: CurrentUser,
                 clock: Clock = system_clock) -> bool:
    """Close an invite. Already closed (accepted, revoked or expired) is a no-op with no audit. True if it changed."""
    invite = await InviteRepository(session).get_for_update(invite_id)
    if invite is None or invite.org_id != org_id:
        raise NotFound("Invitation not found")
    if invite.accepted_at is not None or invite.revoked_at is not None:
        return False
    invite.revoked_at, invite.revoke_reason = clock.now(), InviteRevokeReason.REVOKED.value
    await InviteRepository(session).save(invite)
    await audit_service.record(session, actor, constants.AUDIT_INVITE_REVOKED, "invite", invite.id,
                               {"email": str(invite.email) if invite.email else None}, org_id=org_id, clock=clock)
    return True
