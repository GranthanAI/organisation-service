"""The outbox relay: publishes outbox rows to Kafka, in order, one relay at a time.

Why ONE relay across all instances: per-key order. Two relays could each take a row of the same key and publish them
out of order (a `left` before its `joined`). The relay therefore holds a Postgres advisory lock
(`pg_try_advisory_lock(OUTBOX_LOCK_ID)`) on its own connection while it relays; other instances idle and retry every
5 s, which also makes them take over within seconds if the leader dies (its connection closes, the lock goes).

Each batch takes the oldest unpublished rows (created_at, id), publishes them in that order, and stops at the first
row that is not due yet or that fails: the failure is recorded with exponential backoff (1 s up to 10 min) and later
rows are NOT published ahead of it. `published_at` is set only after the broker acked.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.clients.kafka_producer import EventProducer
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.logging import logger
from app.db.session import unit_of_work
from app.repositories.outbox_repository import OutboxRepository
from app.telemetry.metrics import OUTBOX_OLDEST_AGE, OUTBOX_PENDING, OUTBOX_PUBLISH_FAILURES
from app.workers.backoff import backoff_seconds, sleep_or_stop

OUTBOX_LOCK_ID = 7_240_001      # arbitrary, unique to this service's relay (advisory locks are per database)
LOCK_RETRY_SECONDS = 5.0


class OutboxRelay:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        producer: EventProducer,
        clock: Clock = system_clock,
        batch_size: int | None = None,
        poll_seconds: float | None = None,
        lock_retry_seconds: float = LOCK_RETRY_SECONDS,
    ) -> None:
        self._engine = engine
        self._factory = session_factory
        self._producer = producer
        self._clock = clock
        self._batch = batch_size or settings.OUTBOX_BATCH_SIZE
        self._poll = settings.OUTBOX_POLL_SECONDS if poll_seconds is None else poll_seconds
        self._lock_retry = lock_retry_seconds
        self._lock_conn: AsyncConnection | None = None

    # ---- the leader lock
    @property
    def is_leader(self) -> bool:
        return self._lock_conn is not None

    async def try_lock(self) -> bool:
        if self._lock_conn is not None:
            return True
        conn = await self._engine.connect()
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        try:
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": OUTBOX_LOCK_ID})).scalar_one()
        except BaseException:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return False
        self._lock_conn = conn
        return True

    async def unlock(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is not None:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": OUTBOX_LOCK_ID})
            except Exception:
                pass   # a dead connection has already lost the lock
            await conn.close()

    async def _still_leader(self) -> bool:
        if self._lock_conn is None:
            return False
        try:
            await self._lock_conn.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.warning("outbox relay lost its lock connection")
            await self.unlock()
            return False

    # ---- one batch
    async def publish_batch(self) -> int:
        """Publish what is due, in order. Returns how many rows were published; 0 if this relay is not the leader."""
        if not await self._still_leader():
            return 0
        published = 0
        async with unit_of_work(self._factory) as session:
            repo = OutboxRepository(session)
            for row in await repo.oldest_unpublished(self._batch):
                if row.next_attempt_at > self._clock.now():
                    break   # the head of the queue is waiting out a backoff: nothing behind it may go first
                try:
                    await self._producer.send(row.topic, row.key, row.payload, row.correlation_id)
                except Exception as exc:
                    row.attempts += 1
                    row.last_error = f"{type(exc).__name__}: {exc}"[:500]
                    row.next_attempt_at = self._clock.now() + timedelta(seconds=backoff_seconds(row.attempts))
                    OUTBOX_PUBLISH_FAILURES.inc()
                    logger.warning("outbox publish failed", topic=row.topic, attempts=row.attempts,
                                   error=type(exc).__name__)
                    break
                row.published_at = self._clock.now()   # only after the broker acked
                published += 1
            await session.flush()   # autoflush is off: the stats must see this batch
            pending, oldest_age = await repo.stats(self._clock.now())
        OUTBOX_PENDING.set(pending)
        OUTBOX_OLDEST_AGE.set(oldest_age)
        return published

    # ---- the loop
    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                if not self.is_leader and not await self.try_lock():
                    if await sleep_or_stop(stop, self._lock_retry):
                        break
                    continue
                if await self.publish_batch() >= self._batch:
                    continue   # a full batch: there is probably more
                if await sleep_or_stop(stop, self._poll):
                    break
        finally:
            await self.unlock()

