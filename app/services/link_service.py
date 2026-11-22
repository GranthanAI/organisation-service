"""Invite links (Phase 8): one shareable link for a whole class, with a use limit and an expiry.

A link is an `invites` row: kind `link`, no email, role member, `max_uses`, `uses`. Its token is DERIVED like every invite
token (security/tokens.py), so the URL can be shown again to an admin without storing it. A link sends no email and
publishes no `org.invite.created`. Every function runs in the caller's transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import InviteKind, InviteRevokeReason, Role
from app.core.exceptions import InviteInvalid, NotFound, OrgInactive, ValidationFailed
from app.models.invite import Invite
from app.repositories.domain_repository import DomainRepository
from app.repositories.invite_repository import InviteRepository
from app.repositories.org_repository import OrgRepository
from app.security.models import OrgContext
from app.security.tokens import invite_token, invite_token_hash
from app.services import audit_service, invite_service
from app.utils.ids import uuid7


@dataclass(frozen=True)
class LinkView:
    link_id: uuid.UUID
    created_by: uuid.UUID | None
    created_at: datetime
    expires_at: datetime
    max_uses: int | None
    uses: int
    domain_restricted: bool
    status: str


def link_url(link: Invite) -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/join/{invite_token(link.id, link.token_version)}"


def link_status(link: Invite, now: datetime) -> str:
    """revoked | expired | used_up | open (an expiry closed by the job counts as expired)."""
    if link.revoked_at is not None:
        return "expired" if link.revoke_reason == InviteRevokeReason.EXPIRED.value else "revoked"
    if link.max_uses is not None and link.uses >= link.max_uses:
        return "used_up"
    return "expired" if link.expires_at <= now else "open"


def _view(link: Invite, now: datetime) -> LinkView:
    return LinkView(link.id, link.created_by, link.created_at, link.expires_at, link.max_uses, link.uses,
                    link.domain_restricted, link_status(link, now))


async def _get_link(session: AsyncSession, org_id: uuid.UUID, link_id: uuid.UUID) -> Invite:
    link = await InviteRepository(session).get_for_update(link_id)
    if link is None or link.org_id != org_id or link.kind != InviteKind.LINK.value:
        raise NotFound("Invite link not found")
    return link


async def create(session: AsyncSession, ctx: OrgContext, max_uses: int, expires_in_days: int, domain_restricted: bool,
                 clock: Clock = system_clock) -> tuple[Invite, str]:
    if not 1 <= max_uses <= constants.LINK_MAX_USES:
        raise ValidationFailed(f"max_uses must be between 1 and {constants.LINK_MAX_USES}")
    if not 1 <= expires_in_days <= settings.LINK_MAX_TTL_DAYS:
        raise ValidationFailed(f"expires_in_days must be between 1 and {settings.LINK_MAX_TTL_DAYS}")
    org = await OrgRepository(session).get(ctx.org_id)
    if org is None:
        raise NotFound("Organisation not found")
    if org.status not in invite_service.ACCEPTING:
        raise OrgInactive("this organisation cannot invite right now")
    if domain_restricted and not any(d.verified_at is not None for d in await DomainRepository(session).list_for_org(org.id)):
        raise ValidationFailed("verify a domain first")
    now = clock.now()
    link_id = uuid7()
    link = Invite(
        id=link_id, org_id=org.id, kind=InviteKind.LINK.value, email=None, role=Role.MEMBER.value,
        token_hash=invite_token_hash(link_id, 1), max_uses=max_uses, uses=0, domain_restricted=domain_restricted,
        expires_at=now + timedelta(days=expires_in_days), token_version=1, created_by=ctx.user.id, created_at=now)
    await InviteRepository(session).add(link)
    await audit_service.record(
        session, ctx.user, constants.AUDIT_INVITE_LINK_CREATED, "invite", link.id,
        {"max_uses": max_uses, "domain_restricted": domain_restricted, "expires_at": link.expires_at.isoformat()},
        org_id=org.id, clock=clock)
    return link, link_url(link)


async def list_links(session: AsyncSession, org_id: uuid.UUID, clock: Clock = system_clock) -> list[LinkView]:
    now = clock.now()
    return [_view(link, now) for link in await InviteRepository(session).list_links(org_id)]


async def view_url(session: AsyncSession, ctx: OrgContext, link_id: uuid.UUID, clock: Clock = system_clock) -> str:
    """The link again, for an admin to copy. Open links only (anything else is 404 INVITE_INVALID). Audited."""
    link = await _get_link(session, ctx.org_id, link_id)
    if link_status(link, clock.now()) != "open":
        raise InviteInvalid("This invite link is not open")
    await audit_service.record(session, ctx.user, constants.AUDIT_INVITE_LINK_URL_VIEWED, "invite", link.id, {},
                               org_id=ctx.org_id, clock=clock)
    return link_url(link)


async def revoke(session: AsyncSession, ctx: OrgContext, link_id: uuid.UUID, clock: Clock = system_clock) -> bool:
    """Revoke a link. Already closed (revoked, expired by the job) is a no-op. True if it changed."""
    link = await _get_link(session, ctx.org_id, link_id)
    if link.revoked_at is not None:
        return False
    link.revoked_at, link.revoke_reason = clock.now(), InviteRevokeReason.REVOKED.value
    await InviteRepository(session).save(link)
    await audit_service.record(session, ctx.user, constants.AUDIT_INVITE_REVOKED, "invite", link.id, {"kind": "link"},
                               org_id=ctx.org_id, clock=clock)
    return True
