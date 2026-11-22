"""The Kafka consumer worker: user-events and user.deleted from auth-service.

For each message: decode, dispatch to a handler that runs in ONE transaction (dedupe row + effects), then commit the
offset. The offset is committed only after that transaction commits, or after the message went to the DLQ.

Errors:
  * retryable (database or connection errors): up to CONSUMER_MAX_RETRIES retries after the first try, waiting
    0.5 s, 2 s and 5 s, then the DLQ;
  * everything else (undecodable bytes, schema errors, a handler bug): the DLQ at once, no retries, so one poison
    message cannot stall the partition.
DLQ value: {original_topic, partition, offset, key, value, value_encoding, error, attempts, failed_at}.
"""

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.kafka_consumer import EventConsumer
from app.clients.kafka_producer import EventProducer
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.logging import logger
from app.events import topics
from app.events.schemas import (
    EmailVerifiedPayload,
    SubscriptionChangedPayload,
    UserDeletedEvent,
    UserEventEnvelope,
    UserRegisteredPayload,
    iso_z,
)
from app.services import seat_source
from app.services import user_event_handlers as handlers
from app.telemetry.metrics import CONSUMER_LAG, DLQ_MESSAGES
from app.workers.backoff import sleep_or_stop

RETRY_DELAYS = (0.5, 2.0, 5.0)
DLQ_RETRY_SECONDS = 5.0
LAG_EVERY_SECONDS = 10.0
POLL_SECONDS = 1.0

Sleep = Callable[[float], Awaitable[None]]


def is_retryable(exc: BaseException) -> bool:
    """Database and connection trouble may pass; a bad message never will."""
    if isinstance(exc, (OperationalError, InterfaceError, OSError, TimeoutError)):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


class DeadLetterAborted(RuntimeError):
    """The worker is stopping and the dead letter could not be sent: do NOT commit the offset."""


class ConsumerWorker:
    def __init__(
        self,
        consumer: EventConsumer,
        producer: EventProducer,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock = system_clock,
        sleep: Sleep = asyncio.sleep,
        max_retries: int | None = None,
        dlq_topic: str | None = None,
        topic_aliases: dict[str, str] | None = None,
    ) -> None:
        self._consumer = consumer
        self._producer = producer
        self._factory = session_factory
        self._clock = clock
        self._sleep = sleep
        self._max_retries = settings.CONSUMER_MAX_RETRIES if max_retries is None else max_retries
        self._dlq_topic = dlq_topic or settings.KAFKA_DLQ_TOPIC
        self._aliases = topic_aliases or {}   # real topic name -> logical one (tests use throwaway topics)
        self._stop = asyncio.Event()

    # ---- one message
    async def handle(self, msg: Any) -> None:
        """Handle one message to completion (processed, skipped or dead-lettered), then commit its offset."""
        attempts = 0
        while True:
            attempts += 1
            try:
                await self._dispatch(msg)
                break
            except Exception as exc:
                if not is_retryable(exc):
                    await self._dead_letter(msg, exc, attempts)
                    break
                if attempts > self._max_retries:
                    await self._dead_letter(msg, exc, attempts)
                    break
                delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
                logger.warning("consumer retry", topic=msg.topic, offset=msg.offset, attempt=attempts,
                               error=type(exc).__name__, wait=delay)
                await self._sleep(delay)
        await self._consumer.commit_message(msg)

    async def _dispatch(self, msg: Any) -> None:
        body = json.loads(msg.value)   # undecodable bytes raise here: a schema-level error, no retries
        topic = self._aliases.get(msg.topic, msg.topic)
        if topic == topics.USER_DELETED:
            await handlers.process_user_deleted(self._factory, UserDeletedEvent.model_validate(body), self._clock)
            return
        if topic == topics.SUBSCRIPTION_CHANGED:
            await seat_source.process_subscription_changed(self._factory, SubscriptionChangedPayload.model_validate(body), self._clock)
            return
        if topic != topics.USER_EVENTS:
            raise ValueError(f"unexpected topic {msg.topic}")
        envelope = UserEventEnvelope.model_validate(body)
        if envelope.event_type == "UserRegistered":
            await handlers.process_user_registered(
                self._factory, UserRegisteredPayload.model_validate(envelope.payload), self._clock)
        elif envelope.event_type == "EmailVerified":
            await handlers.process_email_verified(
                self._factory, EmailVerifiedPayload.model_validate(envelope.payload), self._clock)
        else:   # skipped, but recorded so it is not read twice
            event_id = str(envelope.payload.get("event_id") or f"{msg.topic}:{msg.partition}:{msg.offset}")
            await handlers.record_ignored(self._factory, self._clock, event_id)

    # ---- the DLQ
    async def _dead_letter(self, msg: Any, exc: BaseException, attempts: int) -> None:
        value, encoding = _text_or_base64(msg.value)
        letter = {
            "original_topic": msg.topic,
            "partition": msg.partition,
            "offset": msg.offset,
            "key": _text_or_base64(msg.key)[0] if msg.key is not None else None,
            "value": value,
            "value_encoding": encoding,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "attempts": attempts,
            "failed_at": iso_z(self._clock.now()),
        }
        key = letter["key"] or f"{msg.topic}:{msg.partition}:{msg.offset}"
        DLQ_MESSAGES.inc()
        logger.error("message dead-lettered", topic=msg.topic, partition=msg.partition, offset=msg.offset,
                     attempts=attempts, error=type(exc).__name__)
        while True:
            try:
                await self._producer.send(self._dlq_topic, key, letter)
                return
            except Exception as send_exc:
                logger.error("could not send to the DLQ, will retry", error=type(send_exc).__name__)
                if await sleep_or_stop(self._stop, DLQ_RETRY_SECONDS):
                    raise DeadLetterAborted("stopping before the dead letter was sent") from send_exc

    # ---- the loop
    async def run(self, stop: asyncio.Event) -> None:
        self._stop = stop
        await self._consumer.start()
        try:
            next_lag_at = 0.0
            while not stop.is_set():
                if getattr(self._consumer, "lag", None) is not None and time.monotonic() >= next_lag_at:
                    next_lag_at = time.monotonic() + LAG_EVERY_SECONDS
                    try:
                        CONSUMER_LAG.set(await self._consumer.lag())
                    except Exception:
                        logger.warning("could not read the consumer lag")
                msg = await self._consumer.next_message(POLL_SECONDS)
                if msg is None:
                    continue
                try:
                    await self.handle(msg)
                except DeadLetterAborted:
                    break   # leave the offset uncommitted: the message is read again on restart
        finally:
            await self._consumer.stop()


def _text_or_base64(raw: bytes | str | None) -> tuple[str | None, str]:
    if raw is None:
        return None, "utf-8"
    if isinstance(raw, str):
        return raw, "utf-8"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii"), "base64"
