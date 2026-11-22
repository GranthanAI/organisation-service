"""The job registry: one handler per `jobs.kind`. Later phases add theirs here (pilot, invites, domains, csv).

A handler runs in the job's own transaction: its effects and the job's `done_at` commit together, and a failure
rolls the effects back before the runner records the failure.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.constants import IDEMPOTENCY_TTL_HOURS
from app.db.session import unit_of_work
from app.models.job import Job
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.processed_event_repository import ProcessedEventRepository
from app.services import job_service

JobHandler = Callable[[AsyncSession, Job, Clock], Awaitable[None]]

REGISTRY: dict[str, JobHandler] = {}

PROCESSED_EVENTS_RETENTION = timedelta(days=30)
OUTBOX_PUBLISHED_RETENTION = timedelta(days=7)


class DeferJob(Exception):
    """Raised by a handler to run the job again at `run_at` WITHOUT counting an attempt or a failure."""

    def __init__(self, run_at) -> None:
        super().__init__("deferred")
        self.run_at = run_at


def job_handler(kind: str) -> Callable[[JobHandler], JobHandler]:
    def register(fn: JobHandler) -> JobHandler:
        if kind in REGISTRY:
            raise RuntimeError(f"two handlers for job kind {kind!r}")
        REGISTRY[kind] = fn
        return fn

    return register


# ---- housekeeping (Phase 4)
@job_handler("idempotency.cleanup")
async def idempotency_cleanup(session: AsyncSession, job: Job, clock: Clock) -> None:
    await IdempotencyRepository(session).delete_older_than(clock.now() - timedelta(hours=IDEMPOTENCY_TTL_HOURS))


@job_handler("processed_events.cleanup")
async def processed_events_cleanup(session: AsyncSession, job: Job, clock: Clock) -> None:
    await ProcessedEventRepository(session).delete_older_than(clock.now() - PROCESSED_EVENTS_RETENTION)


@job_handler("outbox.cleanup")
async def outbox_cleanup(session: AsyncSession, job: Job, clock: Clock) -> None:
    await OutboxRepository(session).delete_published_before(clock.now() - OUTBOX_PUBLISHED_RETENTION)


# kind -> how often (registered as recurring jobs at startup)
RECURRING: dict[str, timedelta] = {
    "idempotency.cleanup": timedelta(hours=1),
    "processed_events.cleanup": timedelta(days=1),
    "outbox.cleanup": timedelta(days=1),
    "invites.expire": timedelta(days=1),
    "invites.purge": timedelta(days=1),
    "csv.batches.finish": timedelta(minutes=1),
    "domains.verify": timedelta(hours=1),
    "domains.recheck": timedelta(days=1),
    "join_requests.expire": timedelta(days=1),
}


async def ensure_recurring_jobs(session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> list[str]:
    """Queue every recurring job that has no open run. Idempotent; returns the kinds that were added."""
    added = []
    async with unit_of_work(session_factory) as session:
        for kind, every in RECURRING.items():
            if await job_service.ensure_recurring(session, kind, every, clock):
                added.append(kind)
    return added


# Importing these registers their handlers (they use `job_handler` from this module, defined above).
from app.services import domain_jobs, invite_jobs, org_jobs  # noqa: E402,F401
