"""Against the shared dev Kafka (127.0.0.1:9092) and Postgres, with throwaway topics org_test_<8 hex>.*:
outbox -> broker -> a test consumer, and a hand-produced user.deleted ends a seeded membership.

Skipped (with the reason) when Kafka is not reachable. The topics are created and deleted by the test.
"""

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from sqlalchemy import select, text

from app.clients.kafka_consumer import KafkaEventConsumer
from app.clients.kafka_producer import KafkaEventProducer, check_kafka
from app.core.clock import FixedClock
from app.core.config import settings
from app.core.enums import EndReason
from app.db.session import unit_of_work
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import membership_service
from app.workers.consumer_worker import ConsumerWorker
from app.workers.outbox_worker import OutboxRelay
from tests.support.factories import NOW, make_membership, make_org, make_seat_state

BOOTSTRAP = settings.KAFKA_BOOTSTRAP_SERVERS
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def kafka_reachable():
    if not asyncio.run(check_kafka()):
        pytest.skip(f"Kafka is not reachable at {BOOTSTRAP}: start the shared infra (make infra-up)")


@pytest.fixture
async def topic_names(kafka_reachable):
    """org_test_<8 hex>.{org.member.left,user.deleted,dlq}: created here, deleted here, checked gone afterwards."""
    prefix = f"org_test_{uuid.uuid4().hex[:8]}"
    names = {"left": f"{prefix}.org.member.left", "deleted": f"{prefix}.user.deleted", "dlq": f"{prefix}.dlq",
             "group": f"{prefix}-group"}
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(name=names[k], num_partitions=1, replication_factor=1)
                                   for k in ("left", "deleted", "dlq")])
        await asyncio.sleep(1)
        yield names
    finally:
        try:
            await admin.delete_topics([names[k] for k in ("left", "deleted", "dlq")])
            for _ in range(20):
                remaining = [t for t in await admin.list_topics() if t.startswith(prefix)]
                if not remaining:
                    break
                await asyncio.sleep(0.5)
        finally:
            await admin.close()


async def read_all(topic: str, wait_for: int, timeout: float = 15.0) -> list:
    """Read `topic` from the beginning with no consumer group, until `wait_for` messages or the timeout."""
    consumer = AIOKafkaConsumer(bootstrap_servers=BOOTSTRAP, enable_auto_commit=False, group_id=None)
    await consumer.start()
    try:
        tp = TopicPartition(topic, 0)
        consumer.assign([tp])
        await consumer.seek_to_beginning(tp)
        got: list = []
        deadline = asyncio.get_running_loop().time() + timeout
        while len(got) < wait_for and asyncio.get_running_loop().time() < deadline:
            batch = await consumer.getmany(tp, timeout_ms=500)
            got += batch.get(tp, [])
        return got
    finally:
        await consumer.stop()


async def seed(session_factory, roles=("owner", "member")):
    org, users = make_org(), {}
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id))
        for role in roles:
            users[role] = uuid.uuid4()
            await MembershipRepository(s).add(make_membership(org.id, users[role], role=role))
    return org, users


async def point_outbox_at(session_factory, topic_names):
    """The outbox rows carry the real topic name; the test sends them to its throwaway topic instead."""
    async with unit_of_work(session_factory) as s:
        await s.execute(text("UPDATE outbox SET topic = :t WHERE topic = 'org.member.left'"), {"t": topic_names["left"]})


async def test_outbox_to_broker_to_a_test_consumer(engine, session_factory, topic_names):
    org, users = await seed(session_factory)
    clock = FixedClock(NOW)
    async with unit_of_work(session_factory) as s:
        await membership_service.end(s, org.id, users["member"], EndReason.LEFT, "system", clock=clock)
    await point_outbox_at(session_factory, topic_names)

    producer = KafkaEventProducer(BOOTSTRAP)
    await producer.start()
    try:
        relay = OutboxRelay(engine, session_factory, producer, clock)
        assert await relay.try_lock()
        assert await relay.publish_batch() == 1
        await relay.unlock()
    finally:
        await producer.stop()

    (message,) = await read_all(topic_names["left"], 1)
    body = json.loads(message.value)
    assert message.key.decode() == str(users["member"])                      # keyed by user_id
    assert body["org_id"] == str(org.id) and body["user_id"] == str(users["member"])
    assert body["reason"] == "left" and body["org_type"] == "college" and body["role"] == "member"
    assert body["schema_version"] == "1.0" and body["occurred_at"].endswith("Z")
    async with session_factory() as s:
        (row,) = (await s.execute(select(OutboxEvent))).scalars()
    assert row.published_at is not None


async def test_a_hand_produced_user_deleted_ends_the_membership_and_org_member_left_reaches_the_topic(
        engine, session_factory, topic_names):
    org, users = await seed(session_factory)
    member = users["member"]
    event = json.loads((FIXTURES / "user_deleted.json").read_text())
    event.update(user_id=str(member), event_id=str(uuid.uuid4()))
    event.pop("_source")

    # produce the message by hand, exactly as auth-service would (bare JSON, keyed by user_id)
    raw = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP, acks="all")
    await raw.start()
    try:
        await raw.send_and_wait(topic_names["deleted"], json.dumps(event).encode(), key=str(member).encode())
    finally:
        await raw.stop()

    # the consumer worker (real Kafka consumer, throwaway topic and group) handles it
    clock = FixedClock(NOW)
    dlq_producer = KafkaEventProducer(BOOTSTRAP)
    await dlq_producer.start()
    worker = ConsumerWorker(
        KafkaEventConsumer((topic_names["deleted"],), group_id=topic_names["group"], bootstrap_servers=BOOTSTRAP),
        dlq_producer, session_factory, clock, dlq_topic=topic_names["dlq"],
        topic_aliases={topic_names["deleted"]: "user.deleted"})
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        for _ in range(60):
            async with session_factory() as s:
                ended = (await s.execute(select(Membership).where(Membership.user_id == member))).scalar_one()
            if ended.status != "active":
                break
            await asyncio.sleep(0.5)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        await dlq_producer.stop()
    assert (ended.status, ended.end_reason) == ("removed", "user_deleted")

    # the outbox row it wrote is relayed to the broker
    await point_outbox_at(session_factory, topic_names)
    producer = KafkaEventProducer(BOOTSTRAP)
    await producer.start()
    try:
        relay = OutboxRelay(engine, session_factory, producer, clock)
        await relay.try_lock()
        assert await relay.publish_batch() == 1
        await relay.unlock()
    finally:
        await producer.stop()
    (message,) = await read_all(topic_names["left"], 1)
    body = json.loads(message.value)
    assert body["reason"] == "user_deleted" and body["user_id"] == str(member) and body["org_id"] == str(org.id)
    assert message.key.decode() == str(member)
    assert await read_all(topic_names["dlq"], 1, timeout=1.5) == []             # nothing was dead-lettered


async def test_a_poison_message_on_the_wire_reaches_the_dlq_topic(engine, session_factory, topic_names):
    raw = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP, acks="all")
    await raw.start()
    try:
        await raw.send_and_wait(topic_names["deleted"], b"{not json", key=b"k-poison")
    finally:
        await raw.stop()
    dlq_producer = KafkaEventProducer(BOOTSTRAP)
    await dlq_producer.start()
    worker = ConsumerWorker(
        KafkaEventConsumer((topic_names["deleted"],), group_id=topic_names["group"], bootstrap_servers=BOOTSTRAP),
        dlq_producer, session_factory, FixedClock(NOW), dlq_topic=topic_names["dlq"],
        topic_aliases={topic_names["deleted"]: "user.deleted"})
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        (letter,) = await read_all(topic_names["dlq"], 1, timeout=20)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        await dlq_producer.stop()
    body = json.loads(letter.value)
    assert body["original_topic"] == topic_names["deleted"] and body["value"] == "{not json"
    assert body["key"] == "k-poison" and body["attempts"] == 1 and body["offset"] == 0
