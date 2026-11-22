"""Email invites: create, manage (list, resend, revoke), preview (public) and accept.

The preview is the ONLY route here without a JWT (and without the idempotency route class): it is mounted on its own
router, rate-limited per IP. api-gateway must treat exactly GET /org/v1/invites/preview as public.
"""

import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_clock, get_db_session, get_session_factory_dep
from app.api.idempotent_route import IdempotentRoute
from app.core import constants
from app.core.clock import Clock
from app.core.config import settings
from app.core.constants import RATE_CSV_UPLOAD, RATE_INVITE_ACCEPT, RATE_INVITE_CREATE, RATE_INVITE_PREVIEW
from app.core.exceptions import InviteExpired
from app.db.session import unit_of_work
from app.middleware.rate_limit import rate_limit
from app.schemas.org_v1 import (
    AcceptIn,
    AcceptOut,
    BatchOut,
    CreateInvitesIn,
    CreateInvitesOut,
    CreateLinkIn,
    CsvSeatsOut,
    CsvUploadOut,
    InvitePageOut,
    InviteResultOut,
    LinkCreatedOut,
    LinkListOut,
    LinkOut,
    LinkUrlOut,
    PreviewOut,
    SeatsInfoOut,
)
from app.security.dependencies import get_current_user, require_org_role
from app.security.models import CurrentUser, OrgContext
from app.services import csv_service, invite_service, link_service
from app.services.permission_rules import Action
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT

# ---- public: no JWT, no idempotency
public_router = APIRouter(prefix="/invites", tags=["Invites (public)"])


@public_router.get("/preview", response_model=PreviewOut, dependencies=[Depends(rate_limit(RATE_INVITE_PREVIEW))])
async def preview(token: str = Query(min_length=8, max_length=200), session: AsyncSession = Depends(get_db_session),
                  clock: Clock = Depends(get_clock)):
    return await invite_service.preview(session, token, clock)


# ---- logged in
router = APIRouter(tags=["Invites"], route_class=IdempotentRoute)


@router.post("/orgs/{org_id}/invites", response_model=CreateInvitesOut, dependencies=[Depends(rate_limit(RATE_INVITE_CREATE))])
async def create_invites(body: CreateInvitesIn, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                         factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        results, seats = await invite_service.create_invites(session, ctx, body.emails, body.role, clock)
    return CreateInvitesOut(
        results=[InviteResultOut(email=r.email, result=r.result, invite_id=r.invite_id) for r in results],
        seats=SeatsInfoOut(used=seats.used, bought=seats.bought, open_invites=seats.open_invites, warning=seats.warning,
                            payment_due=seats.payment_due))


@router.get("/orgs/{org_id}/invites", response_model=InvitePageOut)
async def list_invites(status: str | None = None, cursor: str | None = None,
                       limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                       ctx: OrgContext = Depends(require_org_role(Action.VIEW_INVITES)),
                       session: AsyncSession = Depends(get_db_session), clock: Clock = Depends(get_clock)):
    return await invite_service.list_invites(session, ctx.org_id, status, cursor, limit, clock)


@router.post("/orgs/{org_id}/invites/{invite_id}/resend", response_model=InviteResultOut)
async def resend_invite(invite_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        invite = await invite_service.resend(session, ctx.org_id, invite_id, ctx.user, clock=clock)
    return InviteResultOut(email=str(invite.email), result="resent", invite_id=invite.id)


@router.delete("/orgs/{org_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(invite_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        await invite_service.revoke(session, ctx.org_id, invite_id, ctx.user, clock)
    return Response(status_code=204)


@router.post("/invites/accept", response_model=AcceptOut, dependencies=[Depends(rate_limit(RATE_INVITE_ACCEPT))])
async def accept_invite(body: AcceptIn, user: CurrentUser = Depends(get_current_user),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    try:
        async with unit_of_work(factory) as session:
            return await invite_service.accept(session, user, body.token, clock)
    except InviteExpired as exc:
        if getattr(exc, "close_invite_id", None) is not None:       # close it, in a transaction of its own, then answer 410
            async with unit_of_work(factory) as session:
                await invite_service.close_expired(session, exc.close_invite_id, clock.now())
        raise


# ---- CSV bulk invites (Phase 8): validated and created inside the request; only the emails go out in the background
@router.post("/orgs/{org_id}/invites/csv", response_model=CsvUploadOut,
             dependencies=[Depends(rate_limit(RATE_CSV_UPLOAD))])
async def upload_csv(file: UploadFile = File(...), role: str = Form("member"), dry_run: bool = Form(False),
                     ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                     factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    data = await file.read(settings.CSV_MAX_BYTES + 1)          # one byte over the limit is enough to refuse it
    async with unit_of_work(factory) as session:
        report = await csv_service.upload(session, ctx, file.filename, data, role.strip().lower(), dry_run, clock)
    return CsvUploadOut(
        batch_id=report.batch_id, dry_run=report.dry_run, rows_total=report.rows_total, rows_ok=report.rows_ok,
        rows_failed=report.rows_failed, errors_preview=report.errors[:constants.CSV_ERRORS_PREVIEW],
        seats=CsvSeatsOut(used=report.used, bought=report.bought, open_invites=report.open_invites, payment_due=report.payment_due))


async def batch_context(batch_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_db_session)) -> csv_service.BatchAccess:
    """The batch and the caller's context in ITS organisation (VIEW_INVITES). Not a member of that org: 404."""
    return await csv_service.batch_for_viewer(session, batch_id, user)


@router.get("/invite-batches/{batch_id}", response_model=BatchOut)
async def get_batch(found: csv_service.BatchAccess = Depends(batch_context),
                    session: AsyncSession = Depends(get_db_session), clock: Clock = Depends(get_clock)):
    batch = found.batch
    sent, pending, not_sent = await csv_service.progress(session, batch, clock.now())
    return BatchOut(batch_id=batch.id, status=batch.status, dry_run=batch.dry_run, rows_total=batch.rows_total,
                    rows_ok=batch.rows_ok, rows_failed=batch.rows_failed, sent=sent, pending_send=pending, not_sent=not_sent,
                    errors_note=batch.errors_note,
                    created_at=batch.created_at, finished_at=batch.finished_at)


@router.get("/invite-batches/{batch_id}/errors.csv")
async def get_batch_errors(found: csv_service.BatchAccess = Depends(batch_context)):
    """The error report: row,email,reason. Every cell goes through safe_cell (no formula runs when it is opened), and
    a UTF-8 BOM makes Excel read it as UTF-8."""
    batch = found.batch
    content = await asyncio.to_thread(csv_service.render_errors_csv, batch.errors or [])
    return Response(
        content=content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="invite-errors-{batch.id}.csv"'})


# ---- invite links
@router.post("/orgs/{org_id}/invite-links", response_model=LinkCreatedOut)
async def create_link(body: CreateLinkIn, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                      factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        link, url = await link_service.create(session, ctx, body.max_uses, body.expires_in_days, body.domain_restricted, clock)
    return LinkCreatedOut(link_id=link.id, url=url, max_uses=link.max_uses, expires_at=link.expires_at,
                          domain_restricted=link.domain_restricted)


@router.get("/orgs/{org_id}/invite-links", response_model=LinkListOut)
async def list_links(ctx: OrgContext = Depends(require_org_role(Action.VIEW_INVITES)),
                     session: AsyncSession = Depends(get_db_session), clock: Clock = Depends(get_clock)):
    return LinkListOut(items=[LinkOut(link_id=v.link_id, created_by=v.created_by, created_at=v.created_at,
                                      expires_at=v.expires_at, max_uses=v.max_uses, uses=v.uses,
                                      domain_restricted=v.domain_restricted, status=v.status)
                              for v in await link_service.list_links(session, ctx.org_id, clock)])


@router.get("/orgs/{org_id}/invite-links/{link_id}/url", response_model=LinkUrlOut)
async def link_url(link_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                   factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:       # the view is audited, so this GET writes
        url = await link_service.view_url(session, ctx, link_id, clock)
    return LinkUrlOut(link_id=link_id, url=url)


@router.delete("/orgs/{org_id}/invite-links/{link_id}", status_code=204)
async def revoke_link(link_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.INVITE)),
                      factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        await link_service.revoke(session, ctx, link_id, clock)
    return Response(status_code=204)
