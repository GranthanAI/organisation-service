"""The job runner: runs due `jobs` rows with the handler registered for their `kind`.

Claiming: due jobs (run_at <= now, not done, not locked by another runner) are taken with FOR UPDATE SKIP LOCKED and
get `locked_until = now + 5 min`, so two runners never run the same job. Each job then runs in its OWN transaction:
the handler's effects and `done_at` commit together.

Failures: attempts+1, `last_error`, and `run_at` pushed back with exponential backoff (1 s, 2 s, 4 s, ...). After 8
failed attempts the job is closed (`done_at` set, `last_error` starting "gave up") and counted in
org_jobs_failed_total{kind}. A recurring job schedules its next run either way, so housekeeping never stops.
"""

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.logging import logger
from app.db.session import unit_of_work
from app.models.job import Job
from app.repositories.job_repository import JobRepository
from app.services import job_service
from app.services.job_handlers import REGISTRY, DeferJob, JobHandler
from app.telemetry.metrics import JOBS_FAILED
from app.utils.ids import uuid7
from app.workers.backoff import backoff_seconds, sleep_or_stop

LOCK_SECONDS = 5 * 60
MAX_ATTEMPTS = 8
CLAIM_BATCH = 10


class JobRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: dict[str, JobHandler] | None = None,
        clock: Clock = system_clock,
        poll_seconds: float | None = None,
    ) -> None:
        self._factory = session_factory
        self._handlers = REGISTRY if handlers is None else handlers
        self._clock = clock
        self._poll = settings.JOB_POLL_SECONDS if poll_seconds is None else poll_seconds

    async def run_once(self) -> int:
        """Claim what is due and run it. Returns the number of jobs attempted."""
        now = self._clock.now()
        async with unit_of_work(self._factory) as session:
            jobs = await JobRepository(session).claim_due(now, CLAIM_BATCH)
            for job in jobs:
                job.locked_until = now + timedelta(seconds=LOCK_SECONDS)
            claimed = [job.id for job in jobs]
        for job_id in claimed:
            await self._run_one(job_id)
        return len(claimed)

    async def _run_one(self, job_id) -> None:
        kind = "?"
        try:
            async with unit_of_work(self._factory) as session:
                job = await session.get(Job, job_id)
                kind = job.kind
                handler = self._handlers.get(job.kind)
                if handler is None:
                    raise LookupError(f"no handler registered for job kind {job.kind!r}")
                await handler(session, job, self._clock)
                job.done_at = self._clock.now()
                job.locked_until = None
                job.last_error = None
                await session.flush()
                await self._schedule_next(session, job)
        except DeferJob as deferred:
            await self._defer(job_id, deferred.run_at)
        except Exception as exc:
            logger.warning("job failed", job_id=str(job_id), kind=kind, error=type(exc).__name__)
            await self._record_failure(job_id, exc)

    async def _defer(self, job_id, run_at) -> None:
        async with unit_of_work(self._factory) as session:
            job = await session.get(Job, job_id)
            job.run_at, job.locked_until = run_at, None          # not a failure: attempts and last_error stay as they were

    async def _record_failure(self, job_id, exc: Exception) -> None:
        async with unit_of_work(self._factory) as session:
            job = await session.get(Job, job_id)
            now = self._clock.now()
            job.attempts += 1
            job.locked_until = None
            error = f"{type(exc).__name__}: {exc}"[:500]
            if job.attempts >= MAX_ATTEMPTS:
                job.done_at = now
                job.last_error = f"gave up: {error}"
                JOBS_FAILED.labels(kind=job.kind).inc()
                logger.error("job given up", kind=job.kind, attempts=job.attempts)
                await session.flush()
                await self._schedule_next(session, job)   # housekeeping must not stop because one run failed 8 times
            else:
                job.last_error = error
                job.run_at = now + timedelta(seconds=backoff_seconds(job.attempts))

    async def _schedule_next(self, session: AsyncSession, job: Job) -> None:
        every = (job.payload or {}).get("every_seconds")
        if job.dedupe_key == job_service.recurring_key(job.kind) and every:
            nxt = Job(id=uuid7(), kind=job.kind, run_at=self._clock.now() + timedelta(seconds=every),
                      payload=job.payload, dedupe_key=job.dedupe_key)
            await JobRepository(session).add_unless_open(nxt)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if await self.run_once() >= CLAIM_BATCH:
                continue
            if await sleep_or_stop(stop, self._poll):
                break
