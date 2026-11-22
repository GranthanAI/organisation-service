"""Invite jobs (Phase 7): sending the email, closing expired invites, purging old ones. Registered in the job registry.

`invite.send {invite_id, token_version}` is queued in the transaction that creates (or resends) the invite, so it runs only
after that commits. It recomputes the token at send time (the raw token is never stored), skips an invite that was
resent since (stale version), revoked, accepted or expired, and raises if the email cannot be sent so the job retries.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock
from app.core.config import settings
from app.core.logging import logger
from app.email.sender import OutgoingEmail, get_email_sender
from app.email.templates import render_invite
from app.models.job import Job
from app.repositories.invite_repository import InviteRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.security.tokens import hash_token, invite_token
from app.services import email_throttle, invite_service
from app.services.job_handlers import DeferJob, job_handler
from app.telemetry.metrics import EMAIL_DEFERRED, EMAIL_SENT

GRANTHANAI = "GranthanAI"


@job_handler(constants.JOB_INVITE_SEND)
async def invite_send(session: AsyncSession, job: Job, clock: Clock) -> None:
    invite_id = uuid.UUID(job.payload["invite_id"])
    version = int(job.payload["token_version"])
    invites = InviteRepository(session)
    invite = await invites.get_for_update(invite_id)
    now = clock.now()
    if invite is None or invite.token_version != version:
        return                                           # stale: a resend replaced this one
    if invite.accepted_at is not None or invite.revoked_at is not None or invite.email is None or invite.expires_at <= now:
        return                                           # nothing to send any more
    org = await OrgRepository(session).get(invite.org_id)
    if org is None or org.status not in invite_service.ACCEPTING:
        return

    until = await email_throttle.acquire(clock)          # the global send rate (N1): nothing is changed when deferred
    if until is not None:
        EMAIL_DEFERRED.inc()
        raise DeferJob(until)
    token = invite_token(invite.id, version)
    if invite.token_hash != hash_token(token):           # an invite made before tokens were derived: fixed at first send
        invite.token_hash = hash_token(token)
        await invites.save(invite)
    inviter = GRANTHANAI                                  # staff-made invites (the owner invite) come from GranthanAI
    if invite.created_by is not None and await MembershipRepository(session).get_active(org.id, invite.created_by):
        inviter = await invite_service.display_name(session, invite.created_by, None) or invite_service.ANONYMOUS_INVITER
    link = f"{settings.APP_BASE_URL.rstrip('/')}/join/{token}"
    email = render_invite(inviter_name=inviter, org_name=org.name, role=invite.role, expires_at=invite.expires_at, link=link)
    await get_email_sender().send(OutgoingEmail(to=str(invite.email), subject=email.subject, text=email.text, html=email.html))
    EMAIL_SENT.inc()
    invite.last_sent_at = now
    await invites.save(invite)
    logger.info("invite email sent", invite_id=str(invite.id), version=version)     # never the link or the address


@job_handler(constants.JOB_INVITES_EXPIRE)
async def invites_expire(session: AsyncSession, job: Job, clock: Clock) -> None:
    """Open invites past expires_at get revoked_at = now and revoke_reason 'expired', in batches of 500."""
    repo, total = InviteRepository(session), 0
    while True:
        closed = await repo.expire_open_before(clock.now(), constants.INVITE_JOB_BATCH)
        total += closed
        if closed < constants.INVITE_JOB_BATCH:
            break
    if total:
        logger.info("expired invites closed", count=total)


@job_handler(constants.JOB_INVITES_PURGE)
async def invites_purge(session: AsyncSession, job: Job, clock: Clock) -> None:
    """Invites never accepted and older than 90 days lose their email address (data minimisation), in batches of 500."""
    from datetime import timedelta
    now = clock.now()
    cutoff = now - timedelta(days=constants.INVITE_PURGE_AFTER_DAYS)
    repo, total = InviteRepository(session), 0
    while True:
        purged = await repo.purge_before(cutoff, now, constants.INVITE_JOB_BATCH)
        total += purged
        if purged < constants.INVITE_JOB_BATCH:
            break
    if total:
        logger.info("old invites purged", count=total)
    cutoff = now - timedelta(days=constants.CSV_ERRORS_PURGE_AFTER_DAYS)           # CSV error rows hold email addresses too
    errors_cleared = 0
    while True:
        cleared = await repo.purge_batch_errors(cutoff, constants.INVITE_JOB_BATCH)
        errors_cleared += cleared
        if cleared < constants.INVITE_JOB_BATCH:
            break
    if errors_cleared:
        logger.info("old csv batch errors cleared", count=errors_cleared)


@job_handler(constants.JOB_CSV_BATCHES_FINISH)
async def csv_batches_finish(session: AsyncSession, job: Job, clock: Clock) -> None:
    """Every minute: a batch that is sending becomes done once no live invite of it is waiting for its email."""
    finished = await InviteRepository(session).finish_batches(clock.now())
    if finished:
        logger.info("csv batches finished", count=finished)
