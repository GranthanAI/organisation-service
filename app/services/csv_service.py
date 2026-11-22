"""CSV bulk invites (Phase 8).

The file is validated AND its invites are created inside the upload request, in ONE transaction. Only the emails are sent
in the background (one `invite.send` job per invite, throttled by the global send rate). The uploaded file is never
stored; only the error rows are kept, on the batch (purged after 90 days with `invites.purge`).

Row checks, in this order (the first that fails is the reason): invalid_email, duplicate_in_file, role_not_allowed,
already_member, in_another_org (never names the org), open_invite_exists (after closing this org's expired open invites
for those emails, the H4 rule), over_seats (valid rows beyond the free seats, in file order).

A request that would take the org past CSV_ROWS_PER_ORG_PER_DAY rows (UTC day) is refused whole (429). `dry_run` writes
only the batch row (status done) and returns the same report.
"""

import asyncio
import csv
import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import InviteKind, OrgStatus, Role
from app.core.exceptions import ForbiddenRole, InvalidEmail, NotFound, OrgInactive, RateLimited, ValidationFailed
from app.events import topics
from app.events.producers import enqueue_many
from app.events.schemas import OrgInviteCreated, iso_z
from app.models.invite import InviteBatch
from app.models.job import Job
from app.repositories.invite_repository import InviteRepository
from app.repositories.job_repository import JobRepository
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser, OrgContext
from app.security.tokens import invite_token_hash
from app.services import audit_service, csv_parse, invite_service, permission_rules
from app.services.email_rules import normalize_email
from app.services.permission_rules import Action
from app.utils.csv_safe import safe_cell
from app.utils.ids import uuid7

INVALID_EMAIL, DUPLICATE_IN_FILE, ROLE_NOT_ALLOWED = "invalid_email", "duplicate_in_file", "role_not_allowed"
ALREADY_MEMBER, IN_ANOTHER_ORG, OPEN_INVITE_EXISTS, OVER_SEATS = (
    "already_member", "in_another_org", "open_invite_exists", "over_seats")
MAX_NAME_LENGTH = 200
MAX_FILENAME_LENGTH = 200


@dataclass(frozen=True)
class CsvReport:
    batch_id: uuid.UUID
    dry_run: bool
    rows_total: int
    rows_ok: int
    rows_failed: int
    errors: list[dict]                 # {row, email, reason}, in row order
    used: int
    bought: int | None
    open_invites: int


def _error(row: csv_parse.CsvRow, email: str, reason: str) -> dict:
    return {"row": row.row, "email": email[:254], "reason": reason}


def _day_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _prevalidate(rows: list[csv_parse.CsvRow], default_role: Role, is_owner: bool):
    """The checks that need no database (email syntax, duplicates in the file, role). Pure and CPU-only."""
    errors: list[dict] = []
    candidates: list[tuple[csv_parse.CsvRow, str, str]] = []        # (row, normalised email, role) that passed 1-3
    seen: set[str] = set()
    for row in rows:
        try:
            email = normalize_email(row.email)
        except InvalidEmail:
            errors.append(_error(row, row.email, INVALID_EMAIL))
            continue
        if email in seen:
            errors.append(_error(row, email, DUPLICATE_IN_FILE))
            continue
        seen.add(email)
        row_role = (row.role or default_role.value).strip().lower()
        if row_role not in (Role.MEMBER.value, Role.ADMIN.value) or (row_role == Role.ADMIN.value and not is_owner):
            errors.append(_error(row, email, ROLE_NOT_ALLOWED))
            continue
        candidates.append((row, email, row_role))
    return errors, candidates


def _build_values(valid, org_id: uuid.UUID, batch_id: uuid.UUID, expires: datetime, created_by: uuid.UUID, now: datetime):
    """The invite rows (ids and derived token hashes): CPU only."""
    values, by_id = [], {}
    for row, email, row_role in valid:
        invite_id = uuid7()
        by_id[invite_id] = (row, email)
        values.append(dict(
            id=invite_id, org_id=org_id, kind=InviteKind.CSV.value, email=email, role=row_role,
            token_hash=invite_token_hash(invite_id, 1), uses=0, batch_id=batch_id, domain_restricted=False,
            expires_at=expires, token_version=1, resend_count=0,
            invitee_name=(row.name or None) and row.name[:MAX_NAME_LENGTH], created_by=created_by, created_at=now))
    return values, by_id


def _build_events(created: list[dict], org, inviter: str | None, expires: datetime, clock: Clock):
    return [(str(org.id), OrgInviteCreated.create(
        clock, org_id=str(org.id), invite_id=str(v["id"]), email=v["email"], role=Role(v["role"]), org_name=org.name,
        inviter_name=inviter, expires_at=iso_z(expires))) for v in created]


def _build_jobs(created: list[dict], now: datetime) -> list[Job]:
    return [Job(id=uuid7(), kind=constants.JOB_INVITE_SEND, run_at=now, attempts=0,
                payload={"invite_id": str(v["id"]), "token_version": 1},
                dedupe_key=f"{constants.JOB_INVITE_SEND}:{v['id']}:1") for v in created]


async def upload(session: AsyncSession, ctx: OrgContext, filename: str | None, data: bytes, role: str, dry_run: bool,
                 clock: Clock = system_clock) -> CsvReport:
    try:
        default_role = Role(role)
    except ValueError as exc:
        raise ValidationFailed("role must be member or admin") from exc
    if default_role not in (Role.MEMBER, Role.ADMIN):
        raise ValidationFailed("role must be member or admin")
    if default_role is Role.ADMIN and ctx.role is not Role.OWNER:
        raise ForbiddenRole("only an owner can invite admins")
    org = await OrgRepository(session).get(ctx.org_id)
    if org is None:
        raise NotFound("Organisation not found")
    if org.status not in invite_service.ACCEPTING:
        raise OrgInactive("this organisation cannot invite right now")

    # CPU-only work (decode, parse, per-row checks, hashing, event building) runs in a thread: the event loop keeps
    # answering other requests while a 5,000-row file is processed (O1)
    rows = await asyncio.to_thread(csv_parse.parse, data, settings.CSV_MAX_BYTES, settings.CSV_MAX_ROWS)
    now = clock.now()
    invites = InviteRepository(session)

    # ---- validate (everything is read in bulk: a 5,000-row file costs a handful of queries)
    errors, candidates = await asyncio.to_thread(_prevalidate, rows, default_role, ctx.role is Role.OWNER)

    emails = [email for _, email, _ in candidates]
    known = await KnownUserRepository(session).get_by_emails(emails)
    active_orgs = await MembershipRepository(session).active_orgs_for_users([u.user_id for u in known.values()])
    await invites.close_expired_for_emails(ctx.org_id, emails, now)
    already_open = await invites.open_emails(ctx.org_id, emails)

    valid: list[tuple[csv_parse.CsvRow, str, str]] = []
    for row, email, row_role in candidates:
        user = known.get(email)
        member_of = active_orgs.get(user.user_id) if user is not None else None
        if member_of is not None:
            errors.append(_error(row, email, ALREADY_MEMBER if member_of == ctx.org_id else IN_ANOTHER_ORG))
        elif email in already_open:
            errors.append(_error(row, email, OPEN_INVITE_EXISTS))
        else:
            valid.append((row, email, row_role))

    members = MembershipRepository(session)
    seat = await SeatStateRepository(session).get(ctx.org_id)
    used = await members.count_active(ctx.org_id)
    open_now = await invites.count_open(ctx.org_id, now)
    bought = None if seat is None or seat.status in invite_service.NO_SEATS else seat.seats_bought
    cap = 0 if seat is None or seat.status in invite_service.NO_SEATS else seat.seats_bought   # None: payg, no cap
    if org.status == OrgStatus.PILOT.value:
        cap = settings.PILOT_MAX_MEMBERS if cap is None else min(cap, settings.PILOT_MAX_MEMBERS)
    if cap is not None:
        free = max(0, cap - used - open_now)
        for row, email, _ in valid[free:]:
            errors.append(_error(row, email, OVER_SEATS))
        valid = valid[:free]

    # ---- the daily row limit: an upload that would cross it is refused as a whole
    created_today = await invites.count_csv_since(ctx.org_id, _day_start(now))
    if created_today + len(valid) > settings.CSV_ROWS_PER_ORG_PER_DAY:
        midnight = _day_start(now) + timedelta(days=1)
        raise RateLimited(int((midnight - now).total_seconds()) + 1)

    batch = InviteBatch(
        id=uuid7(), org_id=ctx.org_id, uploaded_by=ctx.user.id, filename=(filename or "")[:MAX_FILENAME_LENGTH] or None,
        rows_total=len(rows), rows_ok=len(valid), rows_failed=len(errors), dry_run=dry_run, status="done",
        errors=[], created_at=now, finished_at=now)
    if dry_run:
        batch.errors = sorted(errors, key=lambda e: e["row"])
        await invites.add_batch(batch)
        return _report(batch, errors, used, bought, open_now)

    # ---- create (the same transaction): invites, events, send jobs, one audit row
    expires = now + timedelta(days=settings.INVITE_TTL_DAYS)
    values, by_id = await asyncio.to_thread(_build_values, valid, ctx.org_id, batch.id, expires, ctx.user.id, now)
    await invites.add_batch(batch)                                   # before the invites: they reference it
    inserted = await invites.insert_open_ignoring_conflicts(values)
    for invite_id, (row, email) in by_id.items():
        if invite_id not in inserted:                                # lost a race to a concurrent upload or invite
            errors.append(_error(row, email, OPEN_INVITE_EXISTS))
    errors.sort(key=lambda e: e["row"])
    created = [v for v in values if v["id"] in inserted]

    inviter = await invite_service.display_name(session, ctx.user.id, ctx.user.email)
    events = await asyncio.to_thread(_build_events, created, org, inviter, expires, clock)
    await enqueue_many(session, topics.ORG_INVITE_CREATED, events, clock)
    local = settings.INVITE_EMAILS_VIA == "local"
    if local and created:
        await JobRepository(session).add_many(await asyncio.to_thread(_build_jobs, created, now))
    batch.rows_ok, batch.rows_failed, batch.errors = len(created), len(errors), errors
    if local and created:
        batch.status, batch.finished_at = "sending", None           # csv.batches.finish closes it when every email is out
    await invites.save_batch(batch)
    await audit_service.record(
        session, ctx.user, constants.AUDIT_INVITE_CSV_UPLOADED, "invite_batch", batch.id,
        {"batch_id": str(batch.id), "rows_total": batch.rows_total, "rows_ok": batch.rows_ok,
         "rows_failed": batch.rows_failed}, org_id=ctx.org_id, clock=clock)
    return _report(batch, errors, used, bought, await invites.count_open(ctx.org_id, now))


def _report(batch: InviteBatch, errors: list[dict], used: int, bought: int | None, open_invites: int) -> CsvReport:
    return CsvReport(batch.id, batch.dry_run, batch.rows_total, batch.rows_ok, batch.rows_failed,
                     sorted(errors, key=lambda e: e["row"]), used, bought, open_invites)


# ---------------------------------------------------------------------------------------------------- batch status
@dataclass(frozen=True)
class BatchAccess:
    batch: InviteBatch
    ctx: OrgContext


async def batch_for_viewer(session: AsyncSession, batch_id: uuid.UUID, user: CurrentUser) -> BatchAccess:
    """The batch, if the caller is an active member of ITS organisation who may view invites. Anything else about a
    batch that is not theirs (missing, other org) is 404."""
    batch = await InviteRepository(session).get_batch(batch_id)
    membership = None if batch is None else await MembershipRepository(session).get_active(batch.org_id, user.id)
    if batch is None or membership is None:
        raise NotFound("Batch not found")
    role = Role(membership.role)
    permission_rules.check(Action.VIEW_INVITES, role)
    return BatchAccess(batch, OrgContext(org_id=batch.org_id, user=user, role=role, membership_id=membership.id,
                                         action=Action.VIEW_INVITES))


async def progress(session: AsyncSession, batch: InviteBatch, now: datetime) -> tuple[int, int, int]:
    """(sent, pending_send, not_sent) for a batch, derived from the invites. A finished batch has nothing pending."""
    sent, pending, not_sent = await InviteRepository(session).batch_progress(batch.id, now)
    if batch.status != "sending":
        return sent, 0, not_sent + pending
    return sent, pending, not_sent


def render_errors_csv(errors: list[dict]) -> bytes:
    """The error report: row,email,reason, every cell through safe_cell (no formula runs when it is opened), with a UTF-8
    BOM so Excel reads it as UTF-8. CPU only: callers run it in a thread."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["row", "email", "reason"])
    for error in errors:
        writer.writerow([safe_cell(error.get("row")), safe_cell(error.get("email")), safe_cell(error.get("reason"))])
    return ("\ufeff" + out.getvalue()).encode("utf-8")
