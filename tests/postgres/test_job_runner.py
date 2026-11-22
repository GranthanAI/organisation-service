"""The job runner and its housekeeping jobs, with a FixedClock."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.models.idempotency_key import IdempotencyKey
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.models.processed_event import ProcessedEvent
from app.repositories.job_repository import JobRepository
from app.services import job_service
from app.services.job_handlers import RECURRING, REGISTRY, ensure_recurring_jobs
from app.telemetry.metrics import JOBS_FAILED
from app.utils.ids import uuid7
from app.workers.job_runner import MAX_ATTEMPTS, JobRunner
from tests.support.factories import NOW, make_outbox


@pytest.fixture
def clock():
    return FixedClock(NOW)


class Recorder:
    def __init__(self):
        self.ran: list[str] = []
        self.fail: set[str] = set()

    async def __call__(self, session, job, clock):
        self.ran.append(job.kind)
        if job.kind in self.fail:
            session.add(make_outbox())            # an effect that must be rolled back with the failure
            await session.flush()
            raise RuntimeError(f"{job.kind} blew up")


def runner(session_factory, clock, rec, kinds=("a", "b", "boom")):
    return JobRunner(session_factory, {k: rec for k in kinds}, clock, poll_seconds=0.05)


async def schedule(session_factory, kind, run_at, **kw):
    async with unit_of_work(session_factory) as s:
        return await job_service.schedule(s, kind, run_at, **kw)


async def jobs(session_factory, kind=None) -> list[Job]:
    async with session_factory() as s:
        stmt = select(Job).order_by(Job.run_at, Job.id)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        return list((await s.execute(stmt)).scalars())


# --- due, not due, locked ---
async def test_only_due_jobs_run_and_they_are_marked_done(session_factory, clock):
    rec = Recorder()
    await schedule(session_factory, "a", NOW - timedelta(seconds=1))
    await schedule(session_factory, "b", NOW + timedelta(minutes=5))
    assert await runner(session_factory, clock, rec).run_once() == 1
    assert rec.ran == ["a"]
    a, b = await jobs(session_factory, "a"), await jobs(session_factory, "b")
    assert a[0].done_at == NOW and a[0].locked_until is None and a[0].last_error is None
    assert b[0].done_at is None
    clock.advance(minutes=6)
    assert await runner(session_factory, clock, rec).run_once() == 1 and rec.ran == ["a", "b"]


async def test_a_done_job_never_runs_again(session_factory, clock):
    rec = Recorder()
    await schedule(session_factory, "a", NOW)
    r = runner(session_factory, clock, rec)
    assert await r.run_once() == 1
    assert await r.run_once() == 0 and rec.ran == ["a"]


async def test_a_job_locked_by_another_runner_is_skipped(session_factory, clock):
    rec = Recorder()
    await schedule(session_factory, "a", NOW)
    async with unit_of_work(session_factory) as holder:             # another runner has the row locked right now
        held = await JobRepository(holder).claim_due(NOW, 10)
        assert len(held) == 1
        assert await runner(session_factory, clock, rec).run_once() == 0       # SKIP LOCKED: nothing to take
    assert rec.ran == []
    assert await runner(session_factory, clock, rec).run_once() == 1           # released: now it runs


async def test_a_job_with_a_live_lock_waits_until_the_lock_passes(session_factory, clock):
    rec = Recorder()
    await schedule(session_factory, "a", NOW)
    async with unit_of_work(session_factory) as s:
        (job,) = await JobRepository(s).claim_due(NOW, 10)
        job.locked_until = NOW + timedelta(minutes=5)               # claimed by a runner that is still working
    assert await runner(session_factory, clock, rec).run_once() == 0
    clock.advance(minutes=6)                                         # that runner died: the lock expires
    assert await runner(session_factory, clock, rec).run_once() == 1


async def test_two_runners_never_run_the_same_job(session_factory, clock):
    rec = Recorder()
    for _ in range(6):
        await schedule(session_factory, "a", NOW)
    one, two = runner(session_factory, clock, rec), runner(session_factory, clock, rec)
    results = await asyncio.gather(one.run_once(), two.run_once(), one.run_once(), two.run_once())
    assert sum(results) == 6 and len(rec.ran) == 6


# --- failure, backoff, give up ---
async def test_a_failure_counts_an_attempt_backs_off_and_rolls_back_the_handlers_effects(session_factory, clock):
    rec = Recorder()
    rec.fail.add("boom")
    await schedule(session_factory, "boom", NOW)
    r = runner(session_factory, clock, rec)
    await r.run_once()
    (job,) = await jobs(session_factory)
    assert job.attempts == 1 and job.done_at is None and job.locked_until is None
    assert job.last_error == "RuntimeError: boom blew up"
    assert job.run_at == NOW + timedelta(seconds=1)
    async with session_factory() as s:
        assert (await s.execute(select(OutboxEvent))).first() is None          # the handler's write was rolled back
    assert await r.run_once() == 0                                              # still inside the backoff
    clock.advance(seconds=1)
    await r.run_once()
    (job,) = await jobs(session_factory)
    assert job.attempts == 2 and job.run_at == clock.now() + timedelta(seconds=2)


async def test_after_eight_attempts_the_job_is_given_up_and_counted(session_factory, clock):
    rec = Recorder()
    rec.fail.add("boom")
    await schedule(session_factory, "boom", NOW)
    r = runner(session_factory, clock, rec)
    before = JOBS_FAILED.labels(kind="boom")._value.get()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        clock.advance(minutes=30)
        assert await r.run_once() == 1
        (job,) = await jobs(session_factory)
        assert job.attempts == attempt
        assert (job.done_at is not None) == (attempt == MAX_ATTEMPTS)
    assert job.last_error.startswith("gave up") and "boom blew up" in job.last_error
    assert JOBS_FAILED.labels(kind="boom")._value.get() == before + 1
    clock.advance(days=1)
    assert await r.run_once() == 0 and len(rec.ran) == MAX_ATTEMPTS               # never picked up again


async def test_an_unknown_kind_fails_like_any_other_error(session_factory, clock):
    await schedule(session_factory, "nobody-handles-this", NOW)
    await runner(session_factory, clock, Recorder()).run_once()
    (job,) = await jobs(session_factory)
    assert job.attempts == 1 and "no handler registered" in job.last_error


# --- recurring ---
async def test_ensure_recurring_is_idempotent_and_keeps_one_open_job(session_factory, clock):
    async with unit_of_work(session_factory) as s:
        assert await job_service.ensure_recurring(s, "a", timedelta(hours=1), clock) is True
    for _ in range(3):
        async with unit_of_work(session_factory) as s:
            assert await job_service.ensure_recurring(s, "a", timedelta(hours=1), clock) is False
    (job,) = await jobs(session_factory)
    assert job.dedupe_key == "recurring:a" and job.payload == {"every_seconds": 3600}
    assert job.run_at == NOW + timedelta(hours=1)


async def test_a_recurring_job_is_rescheduled_once_after_each_run(session_factory, clock):
    rec = Recorder()
    async with unit_of_work(session_factory) as s:
        await job_service.ensure_recurring(s, "a", timedelta(hours=1), clock)
    r = runner(session_factory, clock, rec)
    assert await r.run_once() == 0                                                  # not due yet
    clock.advance(hours=1)
    assert await r.run_once() == 1
    done, nxt = await jobs(session_factory)
    assert done.done_at is not None and nxt.done_at is None
    assert nxt.run_at == clock.now() + timedelta(hours=1) and nxt.dedupe_key == "recurring:a"
    async with unit_of_work(session_factory) as s:                                  # a restart in between
        assert await job_service.ensure_recurring(s, "a", timedelta(hours=1), clock) is False
    assert len(await jobs(session_factory)) == 2
    clock.advance(hours=1)
    await r.run_once()
    assert len([j for j in await jobs(session_factory) if j.done_at is None]) == 1       # still exactly one open


async def test_a_recurring_job_that_gives_up_still_schedules_the_next_run(session_factory, clock):
    rec = Recorder()
    rec.fail.add("a")
    async with unit_of_work(session_factory) as s:
        await job_service.ensure_recurring(s, "a", timedelta(hours=1), clock)
    r = runner(session_factory, clock, rec)
    for _ in range(MAX_ATTEMPTS):
        clock.advance(hours=2)
        await r.run_once()
    all_jobs = await jobs(session_factory)
    open_jobs = [j for j in all_jobs if j.done_at is None]
    assert len(open_jobs) == 1 and any(j.last_error and j.last_error.startswith("gave up") for j in all_jobs)


async def test_schedule_with_a_dedupe_key_queues_once_and_without_one_queues_every_time(session_factory, clock):
    assert await schedule(session_factory, "a", NOW, dedupe_key="k1") is True
    assert await schedule(session_factory, "a", NOW, dedupe_key="k1") is False
    assert await schedule(session_factory, "a", NOW) is True and await schedule(session_factory, "a", NOW) is True
    assert len(await jobs(session_factory)) == 3


# --- housekeeping ---
async def test_ensure_recurring_jobs_registers_the_three_housekeeping_jobs(session_factory, clock):
    assert set(RECURRING) == {"idempotency.cleanup", "processed_events.cleanup", "outbox.cleanup", "invites.expire",
                              "invites.purge", "csv.batches.finish"}
    assert set(RECURRING) <= set(REGISTRY)
    assert sorted(await ensure_recurring_jobs(session_factory, clock)) == sorted(RECURRING)
    assert await ensure_recurring_jobs(session_factory, clock) == []                 # idempotent at startup
    by_kind = {j.kind: j for j in await jobs(session_factory)}
    assert by_kind["idempotency.cleanup"].run_at == NOW + timedelta(hours=1)
    assert by_kind["processed_events.cleanup"].run_at == NOW + timedelta(days=1)
    assert by_kind["outbox.cleanup"].run_at == NOW + timedelta(days=1)


async def run_job(session_factory, clock, kind):
    await schedule(session_factory, kind, clock.now())
    assert await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once() == 1
    (job,) = [j for j in await jobs(session_factory, kind) if j.last_error is None]
    assert job.done_at is not None, job.last_error


async def test_idempotency_cleanup_deletes_rows_older_than_24_hours(session_factory, clock):
    def row(key, age):
        return IdempotencyKey(actor_id=uuid.uuid4(), route="r", key=key, request_hash="h", state="done",
                              status_code=201, response={}, created_at=NOW - age)

    async with unit_of_work(session_factory) as s:
        s.add_all([row("old-key-1", timedelta(hours=25)), row("old-key-2", timedelta(days=3)),
                   row("new-key-1", timedelta(hours=23)), row("new-key-2", timedelta(minutes=1))])
    await run_job(session_factory, clock, "idempotency.cleanup")
    async with session_factory() as s:
        left = sorted(r.key for r in (await s.execute(select(IdempotencyKey))).scalars())
    assert left == ["new-key-1", "new-key-2"]


async def test_processed_events_cleanup_deletes_rows_older_than_30_days(session_factory, clock):
    async with unit_of_work(session_factory) as s:
        s.add_all([ProcessedEvent(consumer="c", event_id="old", processed_at=NOW - timedelta(days=31)),
                   ProcessedEvent(consumer="c", event_id="edge", processed_at=NOW - timedelta(days=29, hours=23)),
                   ProcessedEvent(consumer="c", event_id="new", processed_at=NOW)])
    await run_job(session_factory, clock, "processed_events.cleanup")
    async with session_factory() as s:
        left = sorted(r.event_id for r in (await s.execute(select(ProcessedEvent))).scalars())
    assert left == ["edge", "new"]


async def test_outbox_cleanup_deletes_only_rows_published_more_than_7_days_ago(session_factory, clock):
    async with unit_of_work(session_factory) as s:
        s.add_all([
            make_outbox(key="old-published", published_at=NOW - timedelta(days=8)),
            make_outbox(key="recent-published", published_at=NOW - timedelta(days=6)),
            make_outbox(key="old-but-unpublished", created_at=NOW - timedelta(days=30)),   # must never be deleted
        ])
    await run_job(session_factory, clock, "outbox.cleanup")
    async with session_factory() as s:
        left = sorted(r.key for r in (await s.execute(select(OutboxEvent))).scalars())
    assert left == ["old-but-unpublished", "recent-published"]


async def test_a_housekeeping_job_reschedules_itself(session_factory, clock):
    await ensure_recurring_jobs(session_factory, clock)
    clock.advance(hours=1)
    assert await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once() == 2     # the hourly one and the every-minute csv.batches.finish are due
    hourly = await jobs(session_factory, "idempotency.cleanup")
    assert [j.done_at is not None for j in hourly] == [True, False]
    assert hourly[1].run_at == clock.now() + timedelta(hours=1)


# --- the loop ---
async def test_run_executes_due_jobs_until_stopped(session_factory, clock):
    rec = Recorder()
    await schedule(session_factory, "a", NOW)
    r = runner(session_factory, clock, rec)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    for _ in range(100):
        if rec.ran:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert rec.ran == ["a"]


def test_registering_two_handlers_for_one_kind_is_an_error():
    from app.services.job_handlers import job_handler
    with pytest.raises(RuntimeError):
        job_handler("outbox.cleanup")(lambda s, j, c: None)
    assert uuid7().version == 7
