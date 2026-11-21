"""The outbox: enqueue in the caller's transaction, ordered publish, backoff, one relay at a time. Fake producer."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.core.clock import FixedClock
from app.core.enums import EndReason, JoinSource, OrgType, Role
from app.db.session import unit_of_work
from app.events import topics
from app.events.producers import enqueue
from app.events.schemas import OrgMemberJoined, OrgMemberLeft, OrgPilotEnding
from app.middleware.correlation import _correlation_id_ctx
from app.models.outbox import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository
from app.telemetry.metrics import OUTBOX_OLDEST_AGE, OUTBOX_PENDING, OUTBOX_PUBLISH_FAILURES
from app.workers.backoff import backoff_seconds
from app.workers.outbox_worker import OutboxRelay
from tests.support.factories import NOW
from tests.support.fakes import FakeProducer

ORG = str(uuid.uuid4())


def joined(user: str, clock) -> OrgMemberJoined:
    return OrgMemberJoined.create(clock, org_id=ORG, user_id=user, role=Role.MEMBER, org_type=OrgType.COLLEGE,
                                  source=JoinSource.INVITE)


def left(user: str, clock) -> OrgMemberLeft:
    return OrgMemberLeft.create(clock, org_id=ORG, user_id=user, role=Role.MEMBER, org_type=OrgType.COLLEGE,
                                reason=EndReason.LEFT)


@pytest.fixture
def clock():
    return FixedClock(NOW)


async def queue(session_factory, clock, count: int, user: str | None = None) -> list[str]:
    """`count` member events, a second apart, one transaction each."""
    user = user or str(uuid.uuid4())
    keys = []
    for i in range(count):
        async with unit_of_work(session_factory) as s:
            event = joined(user, clock) if i % 2 == 0 else left(user, clock)
            topic = topics.ORG_MEMBER_JOINED if i % 2 == 0 else topics.ORG_MEMBER_LEFT
            await enqueue(s, topic, user, event, clock)
            keys.append(event.event_id)
        clock.advance(seconds=1)
    return keys


async def rows(session_factory) -> list[OutboxEvent]:
    async with session_factory() as s:
        return list((await s.execute(select(OutboxEvent).order_by(OutboxEvent.created_at, OutboxEvent.id))).scalars())


def relay_for(engine, session_factory, producer, clock, **kw):
    return OutboxRelay(engine, session_factory, producer, clock, poll_seconds=0.05, lock_retry_seconds=0.05, **kw)


# --- enqueue ---
async def test_enqueue_writes_a_row_in_the_callers_transaction(session_factory, clock):
    user = str(uuid.uuid4())
    token = _correlation_id_ctx.set("cid-enqueue")
    try:
        async with unit_of_work(session_factory) as s:
            row = await enqueue(s, topics.ORG_MEMBER_JOINED, user, joined(user, clock), clock)
    finally:
        _correlation_id_ctx.reset(token)
    (stored,) = await rows(session_factory)
    assert stored.id == row.id and stored.topic == "org.member.joined" and stored.key == user
    assert stored.payload["org_type"] == "college" and stored.payload["user_id"] == user
    assert stored.published_at is None and stored.attempts == 0 and stored.next_attempt_at == NOW
    assert stored.correlation_id == "cid-enqueue"


async def test_enqueue_rolls_back_with_the_caller(session_factory, clock):
    user = str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        async with unit_of_work(session_factory) as s:
            await enqueue(s, topics.ORG_MEMBER_JOINED, user, joined(user, clock), clock)
            await s.flush()
            raise RuntimeError("the use case failed after queueing the event")
    assert await rows(session_factory) == []


async def test_enqueue_never_publishes_or_commits(session_factory, clock):
    producer = FakeProducer()
    user = str(uuid.uuid4())
    async with session_factory() as s:
        await enqueue(s, topics.ORG_MEMBER_JOINED, user, joined(user, clock), clock)
        await s.rollback()
    assert producer.calls == 0 and await rows(session_factory) == []


@pytest.mark.parametrize("topic, key_is_user, ok", [
    (topics.ORG_MEMBER_JOINED, True, True), (topics.ORG_MEMBER_JOINED, False, False),   # keyed by user_id, not org_id
    (topics.ORG_MEMBER_LEFT, False, False),
])
async def test_member_events_must_be_keyed_by_user_id(session_factory, clock, topic, key_is_user, ok):
    user = str(uuid.uuid4())
    event = joined(user, clock) if topic == topics.ORG_MEMBER_JOINED else left(user, clock)
    async with unit_of_work(session_factory) as s:
        if ok:
            await enqueue(s, topic, user if key_is_user else ORG, event, clock)
        else:
            with pytest.raises(ValueError, match="keyed by user_id"):
                await enqueue(s, topic, user if key_is_user else ORG, event, clock)


async def test_enqueue_refuses_unknown_topics_and_mismatched_models(session_factory, clock):
    user = str(uuid.uuid4())
    async with unit_of_work(session_factory) as s:
        with pytest.raises(ValueError, match="not a topic"):
            await enqueue(s, "user.deleted", user, joined(user, clock), clock)
        with pytest.raises(ValueError, match="carries"):
            await enqueue(s, topics.ORG_MEMBER_LEFT, user, joined(user, clock), clock)
        await enqueue(s, topics.ORG_PILOT_ENDING, ORG, OrgPilotEnding.create(
            clock, org_id=ORG, ends_at="2026-12-21T12:00:00Z", days_left=7), clock)       # keyed by org_id
    assert len(await rows(session_factory)) == 1


# --- publishing in order ---
async def test_rows_are_published_in_order_and_marked_published(engine, session_factory, clock):
    ids = await queue(session_factory, clock, 5)
    producer = FakeProducer()
    relay = relay_for(engine, session_factory, producer, clock)
    assert await relay.try_lock()
    assert await relay.publish_batch() == 5
    assert [m["value"]["event_id"] for m in producer.sent] == ids
    assert producer.topics() == ["org.member.joined", "org.member.left"] * 2 + ["org.member.joined"]
    assert all(r.published_at is not None for r in await rows(session_factory))
    assert await relay.publish_batch() == 0 and producer.calls == 5      # nothing is sent twice
    await relay.unlock()


async def test_the_correlation_id_goes_out_with_the_message(engine, session_factory, clock):
    user = str(uuid.uuid4())
    token = _correlation_id_ctx.set("cid-out")
    try:
        await queue(session_factory, clock, 1, user)
    finally:
        _correlation_id_ctx.reset(token)
    await queue(session_factory, clock, 1, str(uuid.uuid4()))         # a row queued with no request
    producer = FakeProducer()
    relay = relay_for(engine, session_factory, producer, clock)
    await relay.try_lock()
    await relay.publish_batch()
    assert [m["correlation_id"] for m in producer.sent] == ["cid-out", None]
    await relay.unlock()


async def test_the_batch_size_limits_one_batch(engine, session_factory, clock):
    await queue(session_factory, clock, 7)
    producer = FakeProducer()
    relay = relay_for(engine, session_factory, producer, clock, batch_size=3)
    await relay.try_lock()
    assert [await relay.publish_batch() for _ in range(4)] == [3, 3, 1, 0]
    await relay.unlock()


# --- failure, backoff, no skipping ahead ---
async def test_a_failure_stops_the_batch_and_sets_backoff_without_skipping_ahead(engine, session_factory, clock):
    ids = await queue(session_factory, clock, 4)
    producer = FakeProducer(fail_on={2})                       # the second send fails once
    relay = relay_for(engine, session_factory, producer, clock)
    await relay.try_lock()
    failures_before = OUTBOX_PUBLISH_FAILURES._value.get()

    assert await relay.publish_batch() == 1                    # row 1 went, row 2 failed, rows 3 and 4 were NOT tried
    assert producer.calls == 2 and [m["value"]["event_id"] for m in producer.sent] == ids[:1]
    first, second, third, fourth = await rows(session_factory)
    assert first.published_at is not None
    assert second.published_at is None and second.attempts == 1 and "ConnectionError" in second.last_error
    assert second.next_attempt_at == clock.now() + timedelta(seconds=1)
    assert third.published_at is None and third.attempts == 0 and fourth.published_at is None
    assert OUTBOX_PUBLISH_FAILURES._value.get() == failures_before + 1

    assert await relay.publish_batch() == 0                    # still inside the 1 s backoff: nothing goes, nothing skips
    assert producer.calls == 2

    clock.advance(seconds=1)
    assert await relay.publish_batch() == 3                    # the failed row first, then the rest, in order
    assert [m["value"]["event_id"] for m in producer.sent] == ids
    await relay.unlock()


async def test_backoff_doubles_up_to_ten_minutes(engine, session_factory, clock):
    assert [backoff_seconds(n) for n in (1, 2, 3, 4, 10, 11, 20)] == [1, 2, 4, 8, 512, 600, 600]
    await queue(session_factory, clock, 1)
    producer = FakeProducer()
    producer.fail_always = True
    relay = relay_for(engine, session_factory, producer, clock)
    await relay.try_lock()
    expected = [1, 2, 4, 8]
    for attempt, wait in enumerate(expected, start=1):
        await relay.publish_batch()
        (row,) = await rows(session_factory)
        assert row.attempts == attempt and row.next_attempt_at == clock.now() + timedelta(seconds=wait)
        clock.advance(seconds=wait)
    await relay.unlock()


async def test_published_at_is_only_set_after_the_broker_acks(engine, session_factory, clock):
    ids = await queue(session_factory, clock, 2)
    seen = []

    async def before_ack(topic, key, value):
        async with session_factory() as other:                   # a different connection, mid-send
            published = (await other.execute(text("SELECT count(*) FROM outbox WHERE published_at IS NOT NULL"))).scalar_one()
        seen.append((value["event_id"], published))

    relay = relay_for(engine, session_factory, FakeProducer(before_ack=before_ack), clock)
    await relay.try_lock()
    await relay.publish_batch()
    assert seen == [(ids[0], 0), (ids[1], 0)]                    # nothing is marked while a send is in flight
    assert all(r.published_at is not None for r in await rows(session_factory))
    await relay.unlock()


async def test_a_failed_send_never_marks_the_row_published(engine, session_factory, clock):
    await queue(session_factory, clock, 1)
    producer = FakeProducer()
    producer.fail_always = True
    relay = relay_for(engine, session_factory, producer, clock)
    await relay.try_lock()
    await relay.publish_batch()
    (row,) = await rows(session_factory)
    assert row.published_at is None
    await relay.unlock()


# --- one relay at a time ---
async def test_two_relays_at_once_only_the_lock_holder_publishes(engine, session_factory, clock):
    await queue(session_factory, clock, 4)
    first_producer, second_producer = FakeProducer(), FakeProducer()
    first = relay_for(engine, session_factory, first_producer, clock)
    second = relay_for(engine, session_factory, second_producer, clock)

    assert await first.try_lock() is True
    assert await second.try_lock() is False
    assert second.is_leader is False
    assert await second.publish_batch() == 0 and second_producer.calls == 0     # not the leader: it does nothing
    assert await first.publish_batch() == 4
    assert second_producer.calls == 0 and first_producer.calls == 4

    await first.unlock()
    assert await second.try_lock() is True                                       # the lock is free again
    await second.unlock()


async def test_an_idle_relay_takes_over_when_the_leader_stops(engine, session_factory, clock):
    await queue(session_factory, clock, 2)
    leader, follower_producer = FakeProducer(), FakeProducer()
    first = relay_for(engine, session_factory, leader, clock)
    second = relay_for(engine, session_factory, follower_producer, clock)
    await first.try_lock()
    stop = asyncio.Event()
    task = asyncio.create_task(second.run(stop))
    await asyncio.sleep(0.3)
    assert follower_producer.calls == 0                                          # idling, retrying the lock
    await first.unlock()
    for _ in range(100):
        if follower_producer.calls == 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert follower_producer.calls == 2 and not second.is_leader                 # run() releases the lock on exit


async def test_run_publishes_until_stopped_and_releases_the_lock(engine, session_factory, clock):
    await queue(session_factory, clock, 3)
    producer = FakeProducer()
    relay = relay_for(engine, session_factory, producer, clock)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(stop))
    for _ in range(100):
        if producer.calls == 3:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert producer.calls == 3 and not relay.is_leader
    other = relay_for(engine, session_factory, FakeProducer(), clock)
    assert await other.try_lock() is True
    await other.unlock()


# --- metrics ---
async def test_the_gauges_show_pending_rows_and_the_age_of_the_oldest(engine, session_factory, clock):
    await queue(session_factory, clock, 3)                      # created at NOW, NOW+1, NOW+2; the clock is now NOW+3
    producer = FakeProducer(fail_on={1})
    relay = relay_for(engine, session_factory, producer, clock)
    await relay.try_lock()
    await relay.publish_batch()                                  # the first send fails: all three stay pending
    assert OUTBOX_PENDING._value.get() == 3 and OUTBOX_OLDEST_AGE._value.get() == 3.0
    clock.advance(seconds=1)
    await relay.publish_batch()
    assert OUTBOX_PENDING._value.get() == 0 and OUTBOX_OLDEST_AGE._value.get() == 0.0
    await relay.unlock()


async def test_repository_stats_and_cleanup(session_factory, clock):
    await queue(session_factory, clock, 3)
    async with unit_of_work(session_factory) as s:
        repo = OutboxRepository(s)
        assert await repo.stats(NOW + timedelta(seconds=10)) == (3, 10.0)
        for row in await repo.oldest_unpublished(2):
            row.published_at = NOW
        await s.flush()
        assert (await repo.stats(NOW))[0] == 1
        assert await repo.delete_published_before(NOW + timedelta(seconds=1)) == 2
    assert len(await rows(session_factory)) == 1
