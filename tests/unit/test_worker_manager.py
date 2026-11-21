"""Supervision: a crashing worker restarts, never raises into the app, stops within the timeout. No database."""

import asyncio
import time

import pytest

from app.telemetry.metrics import WORKER_UP
from app.workers.manager import WorkerManager, supervise


def up(name: str) -> float:
    return WORKER_UP.labels(worker=name)._value.get()


async def test_a_crashing_worker_is_restarted_after_the_delay():
    stop, runs = asyncio.Event(), []

    async def flaky(stop_event):
        runs.append(time.monotonic())
        if len(runs) < 3:
            raise RuntimeError("boom")
        await stop_event.wait()

    task = asyncio.create_task(supervise("t-flaky", flaky, stop, restart_delay=0.1))
    for _ in range(100):
        if len(runs) == 3:
            break
        await asyncio.sleep(0.02)
    assert len(runs) == 3 and runs[1] - runs[0] >= 0.09 and runs[2] - runs[1] >= 0.09      # backoff between restarts
    assert up("t-flaky") == 1
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert up("t-flaky") == 0


async def test_a_crash_is_contained_and_logged_not_raised(caplog):
    stop = asyncio.Event()

    async def crash(_stop):
        raise ValueError("kafka exploded")

    task = asyncio.create_task(supervise("t-crash", crash, stop, restart_delay=0.05))
    await asyncio.sleep(0.2)
    assert not task.done()                  # the supervisor survives every crash
    assert up("t-crash") == 0
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert "worker crashed, restarting" in caplog.text and "t-crash" in caplog.text


async def test_the_gauge_is_one_while_running_and_zero_after():
    stop, started = asyncio.Event(), asyncio.Event()

    async def run(stop_event):
        started.set()
        await stop_event.wait()

    task = asyncio.create_task(supervise("t-gauge", run, stop))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert up("t-gauge") == 1
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert up("t-gauge") == 0


async def test_a_worker_that_returns_early_is_restarted_too():
    stop, runs = asyncio.Event(), []

    async def short(_stop):
        runs.append(1)

    task = asyncio.create_task(supervise("t-short", short, stop, restart_delay=0.05))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(runs) >= 2


async def test_stop_waits_for_workers_then_cancels_the_ones_that_hang(monkeypatch, caplog):
    manager = WorkerManager(restart_delay=0.05)
    cancelled = asyncio.Event()

    async def polite(stop_event):
        await stop_event.wait()

    async def stubborn(_stop):
        try:
            await asyncio.sleep(60)          # ignores the stop event, like a stuck network call
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager._tasks = [
        asyncio.create_task(supervise("t-polite", polite, manager._stop, 0.05), name="worker-polite"),
        asyncio.create_task(supervise("t-stubborn", stubborn, manager._stop, 0.05), name="worker-stubborn"),
    ]
    await asyncio.sleep(0.1)
    started = time.monotonic()
    await manager.stop(timeout=0.5)
    assert 0.4 <= time.monotonic() - started < 3
    assert cancelled.is_set() and manager._tasks == []
    assert "worker did not stop in time" in caplog.text and "worker-stubborn" in caplog.text


async def test_stop_with_nothing_started_is_a_no_op():
    await WorkerManager().stop(timeout=0.1)


async def test_a_cancelled_supervisor_propagates_cancellation():
    async def forever(_stop):
        await asyncio.sleep(60)

    task = asyncio.create_task(supervise("t-cancel", forever, asyncio.Event(), 0.05))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert up("t-cancel") == 0


def test_the_manager_starts_the_three_workers(monkeypatch):
    async def go():
        manager = WorkerManager()

        async def idle(stop_event):
            await stop_event.wait()

        monkeypatch.setattr(manager, "_outbox", idle)
        monkeypatch.setattr(manager, "_consumer", idle)
        monkeypatch.setattr(manager, "_jobs", idle)
        manager.start()
        await asyncio.sleep(0.1)
        names = sorted(t.get_name() for t in manager._tasks)
        ups = {n: up(n) for n in ("outbox", "consumer", "jobs")}
        await manager.stop(timeout=2)
        return names, ups

    names, ups = asyncio.run(go())
    assert names == ["worker-consumer", "worker-jobs", "worker-outbox"] and set(ups.values()) == {1}
