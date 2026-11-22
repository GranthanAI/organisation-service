"""Kafka consumer (aiokafka): manual offset commits, one message at a time.

`EventConsumer` is the small interface the consumer worker uses, so tests pass a fake.
"""

from typing import Any, Protocol

from aiokafka import AIOKafkaConsumer, TopicPartition

from app.core.config import settings


class EventConsumer(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def next_message(self, timeout: float) -> Any | None:
        """The next message, or None if none arrived within `timeout` seconds."""

    async def commit_message(self, message: Any) -> None:
        """Commit the offset after `message` (call only once the message is fully handled)."""


class KafkaEventConsumer:
    def __init__(self, topics: tuple[str, ...], group_id: str | None = None,
                 bootstrap_servers: str | None = None) -> None:
        self._topics = topics
        self._group = group_id or settings.KAFKA_CONSUMER_GROUP
        self._bootstrap = bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS
        self._consumer: AIOKafkaConsumer | None = None

    async def start(self) -> None:
        consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=self._bootstrap,
            group_id=self._group,
            enable_auto_commit=False,        # an offset is committed only after the handler's transaction commits
            auto_offset_reset="earliest",
        )
        await consumer.start()
        self._consumer = consumer

    async def stop(self) -> None:
        if self._consumer is not None:
            consumer, self._consumer = self._consumer, None
            await consumer.stop()

    async def next_message(self, timeout: float) -> Any | None:
        assert self._consumer is not None, "consumer is not started"
        batch = await self._consumer.getmany(timeout_ms=int(timeout * 1000), max_records=1)
        for messages in batch.values():
            if messages:
                return messages[0]
        return None

    async def lag(self) -> int:
        """Messages behind the end of the log, summed over the assigned partitions (org_consumer_lag)."""
        assert self._consumer is not None, "consumer is not started"
        total = 0
        for tp in self._consumer.assignment():
            highwater = self._consumer.highwater(tp)
            if highwater is not None:
                total += max(0, highwater - await self._consumer.position(tp))
        return total

    async def commit_message(self, message: Any) -> None:
        assert self._consumer is not None, "consumer is not started"
        await self._consumer.commit({TopicPartition(message.topic, message.partition): message.offset + 1})
