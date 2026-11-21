"""Staff-run organisations (Phase 5): create, find, suspend, reactivate, extend a pilot, close, add a member.

Every function runs in the CALLER's transaction, checks the matching STAFF_* action in `permission_rules`, and audits
with `actor_kind=staff` and the staff member's reason. Members are added only through `join_service.join`.
"""

import base64
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import (
    InviteKind,
    InviteRevokeReason,
    JoinSource,
    OrgStatus,
    OrgType,
    PlanCode,
    Role,
    SeatSource,
    SeatStatus,
)
from app.core.exceptions import ForbiddenRole, NotFound, ValidationFailed
from app.db.errors import constraint_name
from app.models.invite import Invite
from app.models.organisation import Organisation
from app.models.policy import OrgPolicy
from app.models.seat_state import SeatState
from app.repositories.invite_repository import InviteRepository
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.policy_repository import PolicyRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser
from app.security.tokens import new_token
from app.services import audit_service, job_service, join_service, org_lifecycle_rules
from app.services.email_rules import normalize_email
from app.services.permission_rules import Action, check
from app.utils.ids import uuid7

GSTIN = re.compile(constants.GSTIN_PATTERN)
SLUG_MIN, SLUG_MAX = 3, 60


def require_staff_action(action: Action, actor: CurrentUser) -> None:
    if actor.staff_role is None:
        raise ForbiddenRole("Staff access required")
    check(action, actor.staff_role)


# ------------------------------------------------------------------ validation helpers
def validate_gstin_and_state(gstin: str | None, state_code: str | None) -> tuple[str | None, str | None]:
    if gstin is not None:
        gstin = gstin.strip().upper()
        if not GSTIN.match(gstin):
            raise ValidationFailed("gstin must be 15 characters in the GSTIN format")
        if gstin[:2] not in constants.GST_STATE_CODES:
            raise ValidationFailed("gstin starts with an unknown GST state code")
    if state_code is not None:
        state_code = state_code.strip()
        if state_code not in constants.GST_STATE_CODES:
            raise ValidationFailed("state_code is not a GST state code")
    return gstin, state_code


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:SLUG_MAX].strip("-")
    if len(slug) < SLUG_MIN:
        slug = f"org-{slug}".strip("-")
    return slug[:SLUG_MAX]


def _pilot_job_keys(org_id: uuid.UUID) -> list[tuple[str, str, int | None]]:
    """(kind, dedupe_key, days_left) for the jobs of one pilot."""
    keys = [(constants.JOB_PILOT_REMIND, f"{constants.JOB_PILOT_REMIND}:{org_id}:{days}d", days)
            for days in constants.PILOT_REMIND_DAYS]
    keys.append((constants.JOB_PILOT_EXPIRE, f"{constants.JOB_PILOT_EXPIRE}:{org_id}", None))
    return keys


async def schedule_pilot_jobs(session: AsyncSession, org_id: uuid.UUID, ends_at: datetime, now: datetime) -> None:
    """(Re)schedule pilot.remind at ends-7d and ends-1d and pilot.expire at the end, replacing open ones. A reminder
    whose time has already passed is not scheduled."""
    for kind, key, days in _pilot_job_keys(org_id):
        run_at = ends_at - timedelta(days=days) if days is not None else ends_at
        payload = {"org_id": str(org_id)} | ({"days_left": days} if days is not None else {})
        if days is not None and run_at <= now:
            await job_service.cancel(session, key)
            continue
        await job_service.replace(session, kind, run_at, payload, key)


# ------------------------------------------------------------------ create
@dataclass
class CreatedOrg:
    org: Organisation
    owner_joined: bool
    owner_invite_pending: bool


async def create_org(
    session: AsyncSession,
    actor: CurrentUser,
    *,
    name: str,
    type: str,
    billing_email: str,
    owner_email: str,
    pilot: bool,
    reason: str,
    legal_name: str | None = None,
    gstin: str | None = None,
    state_code: str | None = None,
    clock: Clock = system_clock,
) -> CreatedOrg:
    require_staff_action(Action.STAFF_CREATE_ORG, actor)
    try:
        org_type = OrgType(type)
    except ValueError as exc:
        raise ValidationFailed("type must be college or company") from exc
    gstin, state_code = validate_gstin_and_state(gstin, state_code)
    billing_email, owner_email = normalize_email(billing_email), normalize_email(owner_email)
    name = name.strip()
    if not name:
        raise ValidationFailed("name is required")

    now = clock.now()
    ends_at = now + timedelta(days=settings.PILOT_DAYS) if pilot else None
    orgs = OrgRepository(session)
    base, org = slugify(name), None
    for attempt in range(1, 50):
        slug = base if attempt == 1 else f"{base[:SLUG_MAX - len(str(attempt)) - 1]}-{attempt}"
        if await orgs.get_by_slug(slug) is not None:
            continue
        candidate = Organisation(
            id=uuid7(), name=name, slug=slug, type=org_type.value, legal_name=legal_name, gstin=gstin,
            state_code=state_code, billing_email=billing_email,
            status=(OrgStatus.PILOT if pilot else OrgStatus.ACTIVE).value, pilot_ends_at=ends_at, created_by=actor.id,
            created_at=now, updated_at=now)
        try:
            async with session.begin_nested():    # a slug taken by a concurrent request is retried, not fatal
                org = await orgs.add(candidate)
            break
        except IntegrityError as exc:
            if constraint_name(exc) != "uq_organisations_slug":
                raise
    if org is None:
        raise ValidationFailed("could not find a free slug for this name")

    # seats: a pilot has its cap; any other org has none until staff set them (status 'incomplete' = no seats yet)
    if pilot:
        seat = SeatState(org_id=org.id, plan_code=PlanCode.PILOT.value, variant=org_type.value,
                         seats_bought=settings.PILOT_MAX_MEMBERS, status=SeatStatus.ACTIVE.value, period_end=ends_at,
                         source=SeatSource.STAFF.value, refreshed_at=now)
    else:
        seat = SeatState(org_id=org.id, plan_code=PlanCode.ORG_PRO.value, variant=org_type.value, seats_bought=None,
                         status=SeatStatus.INCOMPLETE.value, period_end=None, source=SeatSource.STAFF.value,
                         refreshed_at=now)
    await SeatStateRepository(session).add(seat)
    await PolicyRepository(session).add(OrgPolicy(org_id=org.id, lock_modes=False, approved_mode_ids=[],
                                                  member_spend_cap_paise=None, version=1, updated_by=actor.id,
                                                  updated_at=now))
    await audit_service.record(
        session, actor, constants.AUDIT_ORG_CREATED, "org", org.id,
        {"name": name, "type": org_type.value, "pilot": pilot, "owner_email": owner_email, "slug": org.slug},
        reason, org_id=org.id, staff=True, clock=clock)

    # the owner: join now if we know them, otherwise leave an open owner invite (nothing is sent before Phase 7)
    known = await KnownUserRepository(session).get_by_email(owner_email)
    owner_joined = owner_invite_pending = False
    if known is not None and known.deleted_at is None:
        await join_service.join(session, org.id, known.user_id, JoinSource.STAFF, Role.OWNER, actor=actor, staff=True,
                                clock=clock)
        owner_joined = True
    else:
        _raw_token, token_hash = new_token()   # only the hash is stored; the raw token is discarded, never returned
        invite = Invite(id=uuid7(), org_id=org.id, kind=InviteKind.EMAIL.value, email=owner_email,
                        role=Role.OWNER.value, token_hash=token_hash, expires_at=now + timedelta(days=settings.INVITE_TTL_DAYS),
                        created_by=actor.id, created_at=now)
        await InviteRepository(session).add(invite)
        await audit_service.record(session, actor, constants.AUDIT_INVITE_CREATED, "invite", invite.id,
                                   {"kind": "email", "role": "owner"}, reason, org_id=org.id, staff=True, clock=clock)
        owner_invite_pending = True

    if pilot:
        await schedule_pilot_jobs(session, org.id, ends_at, now)
    return CreatedOrg(org, owner_joined, owner_invite_pending)


# ------------------------------------------------------------------ read
def encode_cursor(created_at: datetime, org_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{org_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, ident = raw.partition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except Exception as exc:
        raise ValidationFailed("cursor is not valid") from exc


async def search_orgs(session: AsyncSession, q: str | None, status: str | None, cursor: str | None,
                      limit: int = 50) -> tuple[list[Organisation], str | None]:
    if status is not None and status not in {s.value for s in OrgStatus}:
        raise ValidationFailed("status is not an organisation status")
    after = decode_cursor(cursor) if cursor else None
    rows = await OrgRepository(session).search(q=q, status=status, after=after, limit=limit + 1)
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return page, next_cursor


async def org_detail(session: AsyncSession, org_id: uuid.UUID) -> tuple[Organisation, SeatState | None, int]:
    org = await OrgRepository(session).get(org_id)
    if org is None:
        raise NotFound("Organisation not found")
    return (org, await SeatStateRepository(session).get(org_id),
            await MembershipRepository(session).count_active(org_id))


# ------------------------------------------------------------------ members
async def add_member(session: AsyncSession, actor: CurrentUser, org_id: uuid.UUID, user_id: uuid.UUID, role: str,
                     reason: str, clock: Clock = system_clock):
    require_staff_action(Action.STAFF_ADD_MEMBER, actor)
    try:
        role_enum = Role(role)
    except ValueError as exc:
        raise ValidationFailed("role must be owner, admin or member") from exc
    membership = await join_service.join(session, org_id, user_id, JoinSource.STAFF, role_enum, actor=actor, staff=True,
                                         clock=clock)
    return membership


# ------------------------------------------------------------------ lifecycle
async def _locked_org(session: AsyncSession, org_id: uuid.UUID) -> Organisation:
    org = await OrgRepository(session).get_for_update(org_id)
    if org is None:
        raise NotFound("Organisation not found")
    return org


async def _change_status(session: AsyncSession, actor: CurrentUser, org: Organisation, target: OrgStatus, reason: str,
                         clock: Clock, **details) -> None:
    previous = org.status
    org_lifecycle_rules.check_transition(OrgStatus(previous), target)
    org.status, org.updated_at = target.value, clock.now()
    if target is OrgStatus.CLOSED:
        org.closed_at = clock.now()
    await OrgRepository(session).save(org)
    await audit_service.record(
        session, actor, constants.AUDIT_ORG_STATUS_CHANGED, "org", org.id, {"from": previous, "to": target.value, **details},
        reason, org_id=org.id, staff=True, clock=clock)


async def is_pilot_org(session: AsyncSession, org: Organisation) -> bool:
    """Was this organisation on the pilot (as opposed to a paid plan)? Read from its seat plan."""
    seat = await SeatStateRepository(session).get(org.id)
    return seat is not None and seat.plan_code == PlanCode.PILOT.value


async def suspend(session: AsyncSession, actor: CurrentUser, org_id: uuid.UUID, reason: str,
                  clock: Clock = system_clock) -> Organisation:
    require_staff_action(Action.STAFF_SUSPEND, actor)
    org = await _locked_org(session, org_id)
    was_pilot = org.status == OrgStatus.PILOT.value
    await _change_status(session, actor, org, OrgStatus.SUSPENDED, reason, clock, was_pilot=was_pilot,
                         pilot_ends_at=org.pilot_ends_at.isoformat() if org.pilot_ends_at else None)
    return org


async def reactivate(session: AsyncSession, actor: CurrentUser, org_id: uuid.UUID, reason: str,
                     clock: Clock = system_clock) -> Organisation:
    require_staff_action(Action.STAFF_REACTIVATE, actor)
    org = await _locked_org(session, org_id)
    if org.status != OrgStatus.SUSPENDED.value:
        raise ValidationFailed("only a suspended organisation can be reactivated")
    was_pilot = await is_pilot_org(session, org)
    target = org_lifecycle_rules.reactivation_target(was_pilot, org.pilot_ends_at, clock.now())
    await _change_status(session, actor, org, target, reason, clock, was_pilot=was_pilot)
    return org


async def extend_pilot(session: AsyncSession, actor: CurrentUser, org_id: uuid.UUID, days: int, reason: str,
                       clock: Clock = system_clock) -> Organisation:
    require_staff_action(Action.STAFF_EXTEND_PILOT, actor)
    if not 1 <= days <= 30:
        raise ValidationFailed("days must be between 1 and 30")
    org = await _locked_org(session, org_id)
    if org.status not in (OrgStatus.PILOT.value, OrgStatus.PILOT_EXPIRED.value):
        raise ValidationFailed("only a pilot, or an expired pilot, can be extended")
    now = clock.now()
    previous_end = org.pilot_ends_at
    org.pilot_ends_at = max(now, previous_end or now) + timedelta(days=days)
    if org.status == OrgStatus.PILOT_EXPIRED.value:
        await _change_status(session, actor, org, OrgStatus.PILOT, reason, clock, cause="pilot extended")
    org.updated_at = now
    await OrgRepository(session).save(org)
    seat = await SeatStateRepository(session).lock_for_update(org_id)
    if seat is not None and seat.plan_code == PlanCode.PILOT.value:
        seat.period_end = org.pilot_ends_at
        await SeatStateRepository(session).save(seat)
    await schedule_pilot_jobs(session, org_id, org.pilot_ends_at, now)
    await audit_service.record(
        session, actor, constants.AUDIT_ORG_STATUS_CHANGED, "org", org_id,
        {"from": org.status, "to": org.status, "pilot_extended_days": days,
         "old_pilot_ends_at": previous_end.isoformat() if previous_end else None,
         "new_pilot_ends_at": org.pilot_ends_at.isoformat()}, reason, org_id=org_id, staff=True, clock=clock)
    return org


async def close(session: AsyncSession, actor: CurrentUser, org_id: uuid.UUID, reason: str,
                clock: Clock = system_clock) -> Organisation:
    """Closed at once (which blocks joins); open invites are revoked; the memberships are ended in batches by the
    org.close_members job so a large organisation does not hold one huge transaction."""
    require_staff_action(Action.STAFF_CLOSE_ORG, actor)
    org = await _locked_org(session, org_id)
    await _change_status(session, actor, org, OrgStatus.CLOSED, reason, clock)
    now = clock.now()
    revoked = await InviteRepository(session).revoke_open_for_org(org_id, now, InviteRevokeReason.REVOKED.value)
    await job_service.schedule(session, constants.JOB_ORG_CLOSE_MEMBERS, now, {"org_id": str(org_id)},
                               dedupe_key=f"{constants.JOB_ORG_CLOSE_MEMBERS}:{org_id}")
    for _kind, key, _days in _pilot_job_keys(org_id):          # the pilot jobs have nothing left to do
        await job_service.cancel(session, key)
    if revoked:
        await audit_service.record(session, actor, constants.AUDIT_INVITE_REVOKED, "org", org_id,
                                   {"count": revoked, "cause": "organisation closed"}, reason, org_id=org_id,
                                   staff=True, clock=clock)
    return org
