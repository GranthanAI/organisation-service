"""Scheduling jobs. A job is a `jobs` row; the runner (workers/job_runner.py) executes it.

`schedule` adds a one-off job in the caller's transaction. `ensure_recurring` keeps ONE open job per kind
(`dedupe_key = "recurring:<kind>"`, with the interval in the payload); the runner schedules the next run after each
run, so a recurring job is never queued twice and survives restarts.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, system_clock
from app.models.job import Job
from app.repositories.job_repository import JobRepository
from app.utils.ids import uuid7

RECURRING_PREFIX = "recurring:"


def recurring_key(kind: str) -> str:
    return f"{RECURRING_PREFIX}{kind}"


async def schedule(session: AsyncSession, kind: str, run_at: datetime, payload: dict[str, Any] | None = None,
                   dedupe_key: str | None = None) -> bool:
    """Queue a job. With a `dedupe_key`, nothing is added while an open job with that key exists (returns False)."""
    job = Job(id=uuid7(), kind=kind, run_at=run_at, payload=payload or {}, dedupe_key=dedupe_key)
    repo = JobRepository(session)
    if dedupe_key is not None:
        return await repo.add_unless_open(job)
    await repo.add(job)
    return True


async def ensure_recurring(session: AsyncSession, kind: str, every: timedelta,
                           clock: Clock = system_clock) -> bool:
    """Make sure `kind` runs every `every`. Idempotent: safe to call on every startup. True if a job was queued."""
    return await schedule(session, kind, clock.now() + every, {"every_seconds": int(every.total_seconds())},
                          dedupe_key=recurring_key(kind))


async def cancel(session: AsyncSession, dedupe_key: str) -> int:
    """Drop the open job with this dedupe key (a job that is already done is left alone)."""
    return await JobRepository(session).delete_open(dedupe_key)


async def replace(session: AsyncSession, kind: str, run_at: datetime, payload: dict[str, Any] | None,
                  dedupe_key: str) -> bool:
    """Replace the open job with this dedupe key by a new one (for example when a pilot is extended)."""
    await cancel(session, dedupe_key)
    return await schedule(session, kind, run_at, payload, dedupe_key=dedupe_key)
