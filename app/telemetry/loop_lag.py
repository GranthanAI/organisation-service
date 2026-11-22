"""Event-loop lag: a background task sleeps a fixed interval and records how late it wakes up. A busy loop (CPU work in a
request) shows up here before it shows up anywhere else. Gauge `org_event_loop_lag_seconds`."""

import asyncio
import time

from app.telemetry.metrics import EVENT_LOOP_LAG

INTERVAL_SECONDS = 0.5


async def run(stop: asyncio.Event, interval: float = INTERVAL_SECONDS) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        EVENT_LOOP_LAG.set(max(0.0, time.perf_counter() - started - interval))
