"""Jobs that belong to organisations: the pilot reminders and expiry (P5f) and ending a closed organisation's
memberships (P5e). Registered in the job registry (services/job_handlers.py).

A job handler runs in the job's own transaction, with the effects and `done_at` committing together; a failure rolls
them back and the runner retries with backoff.
"""

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import constants
from app.core.clock import Clock
from app.core.enums import EndReason, OrgStatus
from app.core.logging import logger
from app.db.session import unit_of_work
from app.events import topics
from app.events.producers import enqueue
from app.events.schemas import OrgPilotEnding, iso_z
from app.models.job import Job
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.services import audit_service, membership_service, org_lifecycle_rules
from app.services.job_handlers import job_handler


@job_handler(constants.JOB_PILOT_REMIND)
async def pilot_remind(session: AsyncSession, job: Job, clock: Clock) -> None:
    """Enqueue org.pilot.ending {org_id, ends_at, days_left}. Skipped if the org is no longer a pilot (it was converted,
    suspended, expired or closed), or if the pilot was extended so that this reminder is stale."""
    org_id = uuid.UUID(job.payload["org_id"])
    days_left = int(job.payload["days_left"])
    org = await OrgRepository(session).get(org_id)
    if org is None or org.status != OrgStatus.PILOT.value or org.pilot_ends_at is None:
        return
    if org.pilot_ends_at - clock.now() > timedelta(days=days_left, hours=1):
        return   # extended since this job was scheduled: the replacement job will send it
    event = OrgPilotEnding.create(clock, org_id=str(org_id), ends_at=iso_z(org.pilot_ends_at), days_left=days_left)
    await enqueue(session, topics.ORG_PILOT_ENDING, str(org_id), event, clock)


@job_handler(constants.JOB_PILOT_EXPIRE)
async def pilot_expire(session: AsyncSession, job: Job, clock: Clock) -> None:
    """A pilot whose end has passed becomes pilot_expired. Members stay members; join() now refuses (ORG_INACTIVE) and
    /internal/memberships reports "pilot_expired". Skipped if the org is no longer a pilot or the end moved."""
    org_id = uuid.UUID(job.payload["org_id"])
    org = await OrgRepository(session).get_for_update(org_id)
    if org is None or org.status != OrgStatus.PILOT.value:
        return
    now = clock.now()
    if org.pilot_ends_at is None or org.pilot_ends_at > now:
        return
    org_lifecycle_rules.check_transition(OrgStatus.PILOT, OrgStatus.PILOT_EXPIRED)
    org.status, org.updated_at = OrgStatus.PILOT_EXPIRED.value, now
    await OrgRepository(session).save(org)
    await audit_service.record(
        session, "system", constants.AUDIT_ORG_STATUS_CHANGED, "org", org_id,
        {"from": OrgStatus.PILOT.value, "to": OrgStatus.PILOT_EXPIRED.value, "cause": "pilot ended",
         "pilot_ends_at": org.pilot_ends_at.isoformat()}, org_id=org_id, clock=clock)


@job_handler(constants.JOB_ORG_CLOSE_MEMBERS)
async def close_members(session: AsyncSession, job: Job, clock: Clock) -> None:
    """End every active membership of a closed organisation (reason org_closed), CLOSE_BATCH_SIZE per transaction.
    Each batch commits on its own, so it resumes where it stopped after a crash: the job only finishes when no active
    member is left. The job's own session is used only for its bookkeeping."""
    org_id = uuid.UUID(job.payload["org_id"])
    factory = async_sessionmaker(bind=session.bind, expire_on_commit=False, autoflush=False)
    ended = 0
    while True:
        async with unit_of_work(factory) as batch:
            active = await MembershipRepository(batch).list_active_for_org(org_id, limit=constants.CLOSE_BATCH_SIZE)
            for membership in active:
                await membership_service.end(batch, org_id, membership.user_id, EndReason.ORG_CLOSED, "system",
                                             clock=clock)
        ended += len(active)
        if len(active) < constants.CLOSE_BATCH_SIZE:
            break
    logger.info("closed organisation members ended", org_id=str(org_id), ended=ended)
