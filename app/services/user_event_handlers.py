"""What the consumer does with each message from auth-service.

Every `process_*` function runs in ONE transaction whose FIRST statement claims `processed_events (consumer,
event_id)`. A conflict means the event was already handled: nothing else runs. The effects and the dedupe row
commit together, so a crash never leaves an event half-handled or handled twice.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import constants
from app.core.clock import Clock
from app.core.enums import EndReason
from app.core.logging import logger
from app.db.session import unit_of_work
from app.events.schemas import EmailVerifiedPayload, UserDeletedEvent, UserRegisteredPayload
from app.repositories.invite_repository import InviteRepository
from app.repositories.known_user_repository import PLACEHOLDER_DOMAIN, KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.processed_event_repository import ProcessedEventRepository
from app.services import audit_service, membership_service
from app.services.email_rules import normalize_email

CONSUMER_USER_REGISTERED = "user_registered"
CONSUMER_EMAIL_VERIFIED = "email_verified"
CONSUMER_USER_DELETED = "user_deleted"
CONSUMER_IGNORED = "user_events_ignored"

SessionFactory = async_sessionmaker[AsyncSession]


async def process_once(factory: SessionFactory, clock: Clock, consumer: str, event_id: str,
                       apply: Callable[[AsyncSession], Awaitable[None]]) -> bool:
    """Run `apply` once per (consumer, event_id). True if it ran, False if the event was already processed."""
    async with unit_of_work(factory) as session:
        if not await ProcessedEventRepository(session).claim(consumer, event_id, clock.now()):
            return False
        await apply(session)
        return True


# ------------------------------------------------------------------ UserRegistered
async def handle_user_registered(session: AsyncSession, event: UserRegisteredPayload, now: datetime) -> None:
    users = KnownUserRepository(session)
    known = await users.get(event.user_id)
    if known is not None and known.deleted_at is not None:
        return   # a late event must not bring a deleted user back
    # upsert never clears email_verified_at
    await users.upsert(event.user_id, normalize_email(event.email), event.full_name, now)


async def process_user_registered(factory: SessionFactory, event: UserRegisteredPayload, clock: Clock) -> bool:
    return await process_once(factory, clock, CONSUMER_USER_REGISTERED, event.event_id,
                              lambda s: handle_user_registered(s, event, clock.now()))


# ------------------------------------------------------------------ EmailVerified
async def on_email_verified(session: AsyncSession, user_id: UUID, email: str) -> None:
    """Hook for Phase 9: domain auto-join runs here (only when DOMAIN_AUTO_JOIN_ENABLED, and never from the
    unverified UserRegistered event). Does nothing yet."""


async def handle_email_verified(session: AsyncSession, event: EmailVerifiedPayload, now: datetime) -> None:
    users = KnownUserRepository(session)
    known = await users.get(event.user_id)
    if known is not None and known.deleted_at is not None:
        return
    email = normalize_email(event.email)
    await users.set_email_verified(event.user_id, email, event.timestamp, now)   # keeps the earliest time
    await on_email_verified(session, event.user_id, email)


async def process_email_verified(factory: SessionFactory, event: EmailVerifiedPayload, clock: Clock) -> bool:
    return await process_once(factory, clock, CONSUMER_EMAIL_VERIFIED, event.event_id,
                              lambda s: handle_email_verified(s, event, clock.now()))


# ------------------------------------------------------------------ user.deleted
async def handle_user_deleted(session: AsyncSession, event: UserDeletedEvent, clock: Clock) -> None:
    now = clock.now()
    users = KnownUserRepository(session)

    # 1. mark the user deleted (creating the row if we never saw them); remember the email for step 3
    known = await users.get(event.user_id)
    email = known.email if known is not None and not str(known.email).endswith("@" + PLACEHOLDER_DOMAIN) else None
    await users.mark_deleted(event.user_id, now)

    # 2. end the active membership (D1: at most one) with reason user_deleted
    membership = await MembershipRepository(session).get_active_for_user(event.user_id)
    if membership is not None:
        result = await membership_service.end(session, membership.org_id, event.user_id, EndReason.USER_DELETED,
                                              "system", clock=clock)
        # 4. the org lost its last owner: leave a trail for staff to fix, do not block the deletion
        if result.owner_missing:
            await audit_service.record(
                session, "system", constants.AUDIT_ORG_OWNER_MISSING, "org", membership.org_id,
                {"user_id": str(event.user_id), "cause": "user_deleted"}, org_id=membership.org_id, clock=clock)

    # 3. revoke open invites to that email
    if email is not None:
        revoked = await InviteRepository(session).revoke_open_for_email(email, now, "user_deleted")
        if revoked:
            logger.info("revoked open invites for a deleted user", count=revoked)


async def process_user_deleted(factory: SessionFactory, event: UserDeletedEvent, clock: Clock) -> bool:
    return await process_once(factory, clock, CONSUMER_USER_DELETED, event.event_id,
                              lambda s: handle_user_deleted(s, event, clock))


# ------------------------------------------------------------------ anything else on user-events
async def record_ignored(factory: SessionFactory, clock: Clock, event_id: str) -> bool:
    """A user-events type this service does not handle: remember it, so it is not read twice."""
    async def nothing(_session: AsyncSession) -> None:
        return None

    return await process_once(factory, clock, CONSUMER_IGNORED, event_id, nothing)
