"""Starts, supervises and stops the background workers: the outbox relay, the consumer worker and the job runner.

A worker that crashes is logged and restarted after 5 s; it never takes the API down, and /ready does not depend on
them (their health is the `org_worker_up{worker}` gauge). On shutdown the stop event is set, the workers get 10 s to
finish, and then they are cancelled.
"""

import asyncio
from collections.abc import Awaitable, Callable

from app.clients.kafka_consumer import KafkaEventConsumer
from app.clients.kafka_producer import KafkaEventProducer
from app.core.clock import system_clock
from app.core.logging import logger
from app.db.postgres import get_engine
from app.db.session import get_session_factory
from app.events import topics
from app.services.job_handlers import ensure_recurring_jobs
from app.telemetry.metrics import WORKER_UP
from app.workers.backoff import sleep_or_stop
from app.workers.consumer_worker import ConsumerWorker
from app.workers.job_runner import JobRunner
from app.workers.outbox_worker import OutboxRelay

RESTART_DELAY_SECONDS = 5.0
STOP_TIMEOUT_SECONDS = 10.0

Run = Callable[[asyncio.Event], Awaitable[None]]


async def supervise(name: str, run: Run, stop: asyncio.Event, restart_delay: float = RESTART_DELAY_SECONDS) -> None:
    """Run `run(stop)` until stopped; log a crash and start it again after `restart_delay`."""
    while not stop.is_set():
        WORKER_UP.labels(worker=name).set(1)
        try:
            await run(stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("worker crashed, restarting", worker=name, error=type(exc).__name__, exc_info=exc)
        finally:
            WORKER_UP.labels(worker=name).set(0)
        if await sleep_or_stop(stop, restart_delay):
            break


class WorkerManager:
    def __init__(self, restart_delay: float = RESTART_DELAY_SECONDS) -> None:
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._restart_delay = restart_delay

    # ---- the three workers (each builds what it needs, so a restart starts from clean clients)
    async def _outbox(self, stop: asyncio.Event) -> None:
        producer = KafkaEventProducer()
        await producer.start()
        try:
            await OutboxRelay(get_engine(), get_session_factory(), producer).run(stop)
        finally:
            await producer.stop()

    async def _consumer(self, stop: asyncio.Event) -> None:
        producer = KafkaEventProducer()      # for the DLQ
        await producer.start()
        try:
            consumer = KafkaEventConsumer(topics.consumed_topics())
            await ConsumerWorker(consumer, producer, get_session_factory()).run(stop)
        finally:
            await producer.stop()

    async def _jobs(self, stop: asyncio.Event) -> None:
        factory = get_session_factory()
        added = await ensure_recurring_jobs(factory, system_clock)
        if added:
            logger.info("recurring jobs registered", kinds=added)
        await JobRunner(factory).run(stop)

    # ---- lifecycle
    def start(self) -> None:
        for name, run in (("outbox", self._outbox), ("consumer", self._consumer), ("jobs", self._jobs)):
            WORKER_UP.labels(worker=name).set(0)
            self._tasks.append(asyncio.create_task(supervise(name, run, self._stop, self._restart_delay),
                                                   name=f"worker-{name}"))
        logger.info("workers started", workers=[t.get_name() for t in self._tasks])

    async def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        self._stop.set()
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            logger.warning("worker did not stop in time, cancelling", worker=task.get_name())
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("workers stopped")
