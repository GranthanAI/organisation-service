"""The consumer worker with a fake consumer and producer: commit order, retries, DLQ, skipped types, duplicates."""

import asyncio
import base64
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.models.known_user import KnownUser
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.models.processed_event import ProcessedEvent
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import user_event_handlers as handlers
from app.workers import consumer_worker as cw
from app.workers.consumer_worker import ConsumerWorker
from tests.support.factories import NOW, make_membership, make_org, make_seat_state
from tests.support.fakes import FakeConsumer, FakeProducer, message

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
USER = uuid.UUID("0194b7a2-5f3e-7c11-8a41-2d3c4b5a6e70")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def clock():
    return FixedClock(NOW)


class Sleeps:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def worker(session_factory, clock, consumer=None, producer=None, sleeps=None, **kw):
    consumer = consumer or FakeConsumer([])
    producer = producer or FakeProducer()
    sleeps = sleeps or Sleeps()
    w = ConsumerWorker(consumer, producer, session_factory, clock, sleep=sleeps, **kw)
    return w, consumer, producer, sleeps


def db_down():
    return OperationalError("SELECT 1", {}, ConnectionError("connection refused"))


async def known_users(session_factory):
    async with session_factory() as s:
        return list((await s.execute(select(KnownUser))).scalars())


async def table(session_factory, model):
    async with session_factory() as s:
        return list((await s.execute(select(model))).scalars())


# --- offsets ---
async def test_the_offset_is_committed_only_after_the_handlers_transaction_commits(session_factory, clock):
    seen = []

    async def on_commit(msg):
        seen.append(len(await known_users(session_factory)))    # what an outside reader sees at commit time

    consumer = FakeConsumer([], on_commit=on_commit)
    w, *_ = worker(session_factory, clock, consumer)
    msg = message("user-events", fixture("user_event_registered.json"), key="k", offset=41)
    await w.handle(msg)
    assert seen == [1]                                           # the row was already committed
    assert consumer.commits == [("user-events", 0, 41)]


async def test_a_failed_handler_never_commits_the_offset_before_the_dlq_send(session_factory, clock):
    order = []

    async def before_ack(topic, key, value):
        order.append("dlq")

    async def on_commit(msg):
        order.append("commit")

    consumer = FakeConsumer([], on_commit=on_commit)
    w, _, producer, _ = worker(session_factory, clock, consumer, FakeProducer(before_ack=before_ack))
    await w.handle(message("user-events", b"{not json", offset=7))
    assert order == ["dlq", "commit"] and consumer.commits == [("user-events", 0, 7)]


# --- retries ---
async def test_a_retryable_error_is_retried_then_succeeds(session_factory, clock, monkeypatch):
    real = handlers.process_user_registered
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise db_down()
        return await real(*args, **kwargs)

    monkeypatch.setattr(handlers, "process_user_registered", flaky)
    w, consumer, producer, sleeps = worker(session_factory, clock)
    await w.handle(message("user-events", fixture("user_event_registered.json"), offset=1))
    assert calls["n"] == 3 and sleeps.delays == [0.5, 2.0]
    assert len(await known_users(session_factory)) == 1
    assert producer.calls == 0                                   # nothing dead-lettered
    assert consumer.commits == [("user-events", 0, 1)]


async def test_retries_run_out_after_three_and_the_message_goes_to_the_dlq(session_factory, clock, monkeypatch):
    async def always(*args, **kwargs):
        raise db_down()

    monkeypatch.setattr(handlers, "process_user_registered", always)
    w, consumer, producer, sleeps = worker(session_factory, clock)
    await w.handle(message("user-events", fixture("user_event_registered.json"), key="u1", offset=5))
    assert sleeps.delays == [0.5, 2.0, 5.0]                      # three retries after the first try
    (sent,) = producer.sent
    assert sent["topic"] == "organisation.dlq" and sent["value"]["attempts"] == 4
    assert "OperationalError" in sent["value"]["error"]
    assert consumer.commits == [("user-events", 0, 5)]


async def test_max_retries_is_configurable(session_factory, clock, monkeypatch):
    async def always(*args, **kwargs):
        raise db_down()

    monkeypatch.setattr(handlers, "process_user_deleted", always)
    w, _, producer, sleeps = worker(session_factory, clock, max_retries=1)
    await w.handle(message("user.deleted", fixture("user_deleted.json"), offset=1))
    assert sleeps.delays == [0.5] and producer.sent[0]["value"]["attempts"] == 2


# --- poison messages ---
@pytest.mark.parametrize("raw, why", [
    (b"{not json", "JSONDecodeError"),
    (b"\xff\xfe\x00garbage", "UnicodeDecodeError"),
    (json.dumps({"event_type": "UserRegistered"}).encode(), "payload"),            # payload has no user_id: schema error
    (json.dumps([1, 2, 3]).encode(), "ValidationError"),
])
async def test_a_poison_user_event_goes_to_the_dlq_at_once_without_retries(session_factory, clock, raw, why):
    w, consumer, producer, sleeps = worker(session_factory, clock)
    await w.handle(message("user-events", raw, key="k-1", offset=9, partition=3))
    assert sleeps.delays == []                                    # no retries
    (sent,) = producer.sent
    letter = sent["value"]
    assert sent["topic"] == "organisation.dlq"
    assert (letter["original_topic"], letter["partition"], letter["offset"], letter["key"]) == ("user-events", 3, 9, "k-1")
    assert letter["attempts"] == 1 and letter["error"] and letter["failed_at"].endswith("Z")
    assert consumer.commits == [("user-events", 3, 9)]
    assert await known_users(session_factory) == []


async def test_dlq_value_is_text_when_utf8_and_base64_when_not(session_factory, clock):
    w, _, producer, _ = worker(session_factory, clock)
    await w.handle(message("user-events", b"plain text, not json", offset=1))
    await w.handle(message("user-events", b"\xff\xfe\x00raw bytes", offset=2))
    text_letter, binary_letter = (m["value"] for m in producer.sent)
    assert (text_letter["value"], text_letter["value_encoding"]) == ("plain text, not json", "utf-8")
    assert binary_letter["value_encoding"] == "base64"
    assert base64.b64decode(binary_letter["value"]) == b"\xff\xfe\x00raw bytes"


async def test_a_bad_user_deleted_goes_to_the_dlq(session_factory, clock):
    w, consumer, producer, sleeps = worker(session_factory, clock)
    body = {**fixture("user_deleted.json"), "user_id": "not-a-uuid"}
    await w.handle(message("user.deleted", body, offset=2))
    assert sleeps.delays == [] and len(producer.sent) == 1 and consumer.commits == [("user.deleted", 0, 2)]


async def test_a_handler_bug_is_dead_lettered_not_retried_forever(session_factory, clock, monkeypatch):
    async def buggy(*args, **kwargs):
        raise KeyError("oops")

    monkeypatch.setattr(handlers, "process_user_deleted", buggy)
    w, _, producer, sleeps = worker(session_factory, clock)
    await w.handle(message("user.deleted", fixture("user_deleted.json"), offset=3))
    assert sleeps.delays == [] and producer.sent[0]["value"]["attempts"] == 1


async def test_a_dlq_send_that_fails_is_retried_before_the_offset_is_committed(session_factory, clock, monkeypatch):
    monkeypatch.setattr(cw, "DLQ_RETRY_SECONDS", 0.01)
    w, consumer, producer, _ = worker(session_factory, clock, producer=FakeProducer(fail_on={1, 2}))
    await w.handle(message("user-events", b"{bad", offset=4))
    assert producer.calls == 3 and len(producer.sent) == 1 and consumer.commits == [("user-events", 0, 4)]


# --- skipped and duplicate ---
async def test_an_unknown_event_type_is_skipped_and_recorded(session_factory, clock):
    w, consumer, producer, sleeps = worker(session_factory, clock)
    body = fixture("user_event_password_reset.json")
    await w.handle(message("user-events", body, offset=11))
    await w.handle(message("user-events", body, offset=12))        # the same event again
    assert producer.calls == 0 and sleeps.delays == []
    assert consumer.commits == [("user-events", 0, 11), ("user-events", 0, 12)]
    (row,) = await table(session_factory, ProcessedEvent)
    assert (row.consumer, row.event_id) == ("user_events_ignored", body["payload"]["event_id"])
    assert await known_users(session_factory) == []


async def test_an_unknown_event_type_without_an_event_id_is_recorded_by_position(session_factory, clock):
    w, *_ = worker(session_factory, clock)
    await w.handle(message("user-events", {"event_type": "SomethingNew", "payload": {}}, offset=21, partition=2))
    (row,) = await table(session_factory, ProcessedEvent)
    assert row.event_id == "user-events:2:21"


async def test_a_redelivered_message_is_processed_once_but_committed_both_times(session_factory, clock):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id))
        await MembershipRepository(s).add(make_membership(org.id, USER, role="member"))
    w, consumer, producer, _ = worker(session_factory, clock)
    msg = message("user.deleted", fixture("user_deleted.json"), key=str(USER), offset=30)
    await w.handle(msg)
    await w.handle(msg)
    assert len(await table(session_factory, OutboxEvent)) == 1
    assert consumer.commits == [("user.deleted", 0, 30)] * 2 and producer.calls == 0
    assert (await table(session_factory, Membership))[0].status == "removed"


# --- the loop ---
async def test_run_processes_messages_in_order_starts_and_stops_the_consumer(session_factory, clock):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id))
        await MembershipRepository(s).add(make_membership(org.id, USER, role="member"))
    consumer = FakeConsumer([
        message("user-events", fixture("user_event_registered.json"), offset=1),
        message("user-events", fixture("user_event_email_verified.json"), offset=2),
        message("user.deleted", fixture("user_deleted.json"), offset=3),
    ])
    w, _, producer, _ = worker(session_factory, clock, consumer)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for _ in range(200):
        if len(consumer.commits) == 3:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert consumer.started and consumer.stopped
    assert [c[2] for c in consumer.commits] == [1, 2, 3]
    (ku,) = await known_users(session_factory)
    assert ku.deleted_at == NOW and ku.email_verified_at is not None
    assert [e.topic for e in await table(session_factory, OutboxEvent)] == ["org.member.left"]
    assert producer.calls == 0


async def test_a_stop_during_a_failing_dlq_send_leaves_the_offset_uncommitted(session_factory, clock, monkeypatch):
    monkeypatch.setattr(cw, "DLQ_RETRY_SECONDS", 30)
    producer = FakeProducer()
    producer.fail_always = True
    consumer = FakeConsumer([message("user-events", b"{bad", offset=50)])
    w, *_ = worker(session_factory, clock, consumer, producer)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert consumer.commits == [] and consumer.stopped          # the message will be read again after a restart
