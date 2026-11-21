"""Create the Kafka topics this service produces, on KAFKA_BOOTSTRAP_SERVERS. Safe to run twice: a topic that exists is
left alone (its partitions are never changed). Prints what it created or found.

    uv run python -m scripts.create_topics                      # 3 partitions, replication factor 1
    uv run python -m scripts.create_topics --partitions 6 --replication-factor 1
"""

import argparse
import asyncio
import sys

from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.core.config import settings
from app.events import topics

TOPICS = [*topics.PRODUCED_TOPICS, topics.DLQ_TOPIC]


async def main(partitions: int, replication: int) -> int:
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [t for t in TOPICS if t not in existing]
        if missing:
            await admin.create_topics([NewTopic(name=t, num_partitions=partitions, replication_factor=replication)
                                       for t in missing])
        for name in TOPICS:
            print(f"{'created' if name in missing else 'found  '}  {name}")
        after = set(await admin.list_topics())
        absent = [t for t in TOPICS if t not in after]
        if absent:
            print(f"NOT PRESENT after creating: {absent}", file=sys.stderr)
            return 1
        print(f"{len(missing)} created, {len(TOPICS) - len(missing)} already existed "
              f"({settings.KAFKA_BOOTSTRAP_SERVERS})")
        return 0
    finally:
        await admin.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--partitions", type=int, default=3)
    parser.add_argument("--replication-factor", type=int, default=1)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.partitions, args.replication_factor)))
