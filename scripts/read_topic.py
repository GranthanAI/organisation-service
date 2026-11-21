"""Read a topic from the beginning (no consumer group, nothing is committed) and print the messages as JSON lines.

    uv run python -m scripts.read_topic org.member.left --contains <user_id> [--seconds 6]
"""

import argparse
import asyncio
import json

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient

from app.core.config import settings


async def main(topic: str, contains: str | None, seconds: float) -> int:
    consumer = AIOKafkaConsumer(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS, enable_auto_commit=False,
                                group_id=None)
    await consumer.start()
    found = 0
    try:
        admin = AIOKafkaAdminClient(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)
        await admin.start()
        try:
            (described,) = await admin.describe_topics([topic])
        finally:
            await admin.close()
        tps = [TopicPartition(topic, p["partition"]) for p in described["partitions"]]
        consumer.assign(tps)
        await consumer.seek_to_beginning(*tps)
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            batch = await consumer.getmany(timeout_ms=500)
            for messages in batch.values():
                for m in messages:
                    text = m.value.decode("utf-8", "replace")
                    if contains and contains not in text and contains != (m.key or b"").decode():
                        continue
                    found += 1
                    print(json.dumps({"topic": m.topic, "partition": m.partition, "offset": m.offset,
                                      "key": (m.key or b"").decode(), "value": json.loads(text)}))
    finally:
        await consumer.stop()
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("topic")
    parser.add_argument("--contains")
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()
    count = asyncio.run(main(args.topic, args.contains, args.seconds))
    raise SystemExit(0 if count else 1)
