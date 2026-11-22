"""Replay dead letters: read organisation.dlq from the start (no consumer group, nothing is committed) and produce each letter's
original value back to its original topic, with its original key. Safe to repeat: the consumer deduplicates by event id
(processed_events), so a message that was handled in the meantime is skipped.

    uv run python -m scripts.replay_dlq --dry-run                       # list what would be replayed
    uv run python -m scripts.replay_dlq                                 # replay everything
    uv run python -m scripts.replay_dlq --contains <user_id or text>    # only letters mentioning it

Fix the cause first (a handler bug, a database outage): a letter that fails again just goes back to the DLQ.
"""

import argparse
import asyncio
import base64
import json

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient

from app.core.config import settings


def original_bytes(letter: dict) -> bytes:
    value = letter["value"] or ""
    return base64.b64decode(value) if letter.get("value_encoding") == "base64" else value.encode("utf-8")


async def main(contains: str | None, dry_run: bool, seconds: float) -> int:
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)
    await admin.start()
    try:
        (described,) = await admin.describe_topics([settings.KAFKA_DLQ_TOPIC])
    finally:
        await admin.close()
    consumer = AIOKafkaConsumer(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS, enable_auto_commit=False, group_id=None)
    producer = AIOKafkaProducer(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS, acks="all")
    await consumer.start()
    await producer.start()
    replayed = 0
    try:
        tps = [TopicPartition(settings.KAFKA_DLQ_TOPIC, p["partition"]) for p in described["partitions"]]
        consumer.assign(tps)
        await consumer.seek_to_beginning(*tps)
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            for messages in (await consumer.getmany(timeout_ms=500)).values():
                for m in messages:
                    letter = json.loads(m.value)
                    if contains and contains not in m.value.decode("utf-8", "replace"):
                        continue
                    print(json.dumps({"dlq_offset": m.offset, "topic": letter["original_topic"], "key": letter.get("key"),
                                      "error": letter.get("error"), "replayed": not dry_run}))
                    if not dry_run:
                        key = letter["key"].encode("utf-8") if letter.get("key") else None
                        await producer.send_and_wait(letter["original_topic"], original_bytes(letter), key=key)
                    replayed += 1
    finally:
        await consumer.stop()
        await producer.stop()
    return replayed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--contains")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()
    print(f"{asyncio.run(main(args.contains, args.dry_run, args.seconds))} letter(s)")
