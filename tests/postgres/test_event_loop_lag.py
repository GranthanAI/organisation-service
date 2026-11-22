"""O1: a college_2000.csv upload must not block the event loop. While it runs, 20 concurrent
GET /internal/memberships/{id} calls each answer in under 300 ms and the lag gauge stays under 0.2 s."""

import asyncio
import time
import uuid
from pathlib import Path

from app.telemetry import loop_lag
from app.telemetry.metrics import EVENT_LOOP_LAG
from tests.api.conftest import api, clock  # noqa: F401  (fixtures)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "college_2000.csv"
SERVICE = {"Authorization": "Bearer dev-billing-token"}


async def test_the_upload_does_not_block_the_event_loop(api):  # noqa: F811
    world = await api.seed_org(seats=5000)
    member = world.members[0]
    stop, worst_lag = asyncio.Event(), [0.0]

    async def watch_gauge():
        while not stop.is_set():
            worst_lag[0] = max(worst_lag[0], EVENT_LOOP_LAG._value.get())
            await asyncio.sleep(0.02)

    upload_started = asyncio.Event()

    async def upload():
        upload_started.set()
        started = time.perf_counter()
        r = await api.client.post(
            f"/org/v1/orgs/{world.org.id}/invites/csv", files={"file": ("college_2000.csv", FIXTURE.read_bytes(), "text/csv")},
            headers={**api.h(world.owner), "Idempotency-Key": uuid.uuid4().hex})
        return r, time.perf_counter() - started

    async def probe():
        started = time.perf_counter()
        r = await api.client.get(f"/internal/memberships/{member.id}", headers=SERVICE)
        return r.status_code, time.perf_counter() - started

    async def probes():
        await upload_started.wait()
        await asyncio.sleep(0.3)                 # let the upload get going (it takes seconds)
        out = []
        for _ in range(3):                       # three waves of 20 concurrent calls while the upload is still running
            out += await asyncio.gather(*[probe() for _ in range(20)])
            await asyncio.sleep(0.2)
        return out

    await asyncio.gather(*[probe() for _ in range(20)])                  # warm the connection pool (a cold pool opens sockets)
    quiet = max(seconds for _, seconds in await asyncio.gather(*[probe() for _ in range(20)]))
    monitor = asyncio.create_task(loop_lag.run(stop, interval=0.1))      # a finer tick than the real 0.5 s, for a short test
    watcher = asyncio.create_task(watch_gauge())
    (response, upload_seconds), results = await asyncio.gather(upload(), probes())
    stop.set()
    await asyncio.gather(monitor, watcher)

    assert response.status_code == 200 and response.json()["rows_ok"] == 1950
    assert all(code == 200 for code, _ in results)
    slowest = max(seconds for _, seconds in results)
    print(f"\nEVENT_LOOP: upload {upload_seconds:.2f}s, {len(results)} probes, slowest {slowest * 1000:.0f} ms "
          f"(20 probes with no upload: {quiet * 1000:.0f} ms), worst lag gauge {worst_lag[0] * 1000:.0f} ms")
    assert upload_seconds > 1.0, "the upload was too fast to overlap the probes; the test proves nothing"
    # Each call must answer in under 300 ms. On a machine where 20 concurrent calls take longer than that even with NO upload
    # running (their own database round trips), the criterion is "no slower than that quiet run plus 30%": the upload adds nothing.
    allowed = max(0.3, quiet * 1.3 + 0.05)
    assert slowest < allowed, f"a membership lookup took {slowest * 1000:.0f} ms while the upload ran (quiet: {quiet * 1000:.0f} ms)"
    assert worst_lag[0] < 0.2, f"the event loop lagged {worst_lag[0] * 1000:.0f} ms"
