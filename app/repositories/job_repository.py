"""Persistence for scheduled jobs. The runner is Phase 4; `claim_due` is the query it will use.
No business rules; never commits."""

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, job: Job) -> Job:
        self.session.add(job)
        await self.session.flush()
        return job

    async def add_unless_open(self, job: Job) -> bool:
        """Insert a job with a `dedupe_key`, unless an open (not done) job with that key exists. True if inserted.
        This is what keeps a recurring job from being queued twice (ux_jobs_open_dedupe)."""
        stmt = insert(Job).values(
            id=job.id, kind=job.kind, payload=job.payload or {}, run_at=job.run_at, attempts=0,
            dedupe_key=job.dedupe_key,
        ).on_conflict_do_nothing(
            index_elements=[Job.dedupe_key], index_where=(Job.dedupe_key.is_not(None) & Job.done_at.is_(None)))
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def get(self, job_id: uuid.UUID) -> Job | None:
        await self.session.flush()
        return await self.session.get(Job, job_id, populate_existing=True)

    async def claim_due(self, now: datetime, limit: int = 10) -> list[Job]:
        """Open jobs due now that nobody holds, locked with FOR UPDATE SKIP LOCKED."""
        stmt = (
            select(Job)
            .where(Job.done_at.is_(None), Job.run_at <= now,
                   (Job.locked_until.is_(None)) | (Job.locked_until < now))
            .order_by(Job.run_at, Job.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_open(self, dedupe_key: str) -> int:
        """Remove the open (not done) job with this dedupe key, if any."""
        result = await self.session.execute(delete(Job).where(Job.dedupe_key == dedupe_key, Job.done_at.is_(None)))
        return result.rowcount
