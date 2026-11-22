"""A college_2000.csv upload while 20 callers hammer GET /internal/memberships/{id}: the p95 DURING the upload against the p95 of a
quiet stretch just before it. Over HTTP, against a running service.

    uv run python -m tests.load.csv_under_load [--base http://127.0.0.1:8010]
"""

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

from tests.load.common import bearer, seed_org
from tests.load.stats import line, summarise

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "college_2000.csv"
SERVICE = {"Authorization": "Bearer dev-billing-token"}
CALLERS = 20


async def main(base: str) -> None:
    from scripts import seed_dev
    member = (await seed_dev.main())["member_id"]
    org_id, owner, email = await seed_org(seats=5000, tag=uuid.uuid4().hex[:8])
    url = f"{base}/internal/memberships/{member}"
    samples: list[tuple[float, float]] = []                              # (finished at, seconds)
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=CALLERS + 2, max_keepalive_connections=CALLERS + 2)
    async with httpx.AsyncClient(limits=limits, timeout=60) as client:
        async def caller():
            while not stop.is_set():
                began = time.perf_counter()
                await client.get(url, headers=SERVICE)
                samples.append((time.perf_counter(), time.perf_counter() - began))

        callers = [asyncio.create_task(caller()) for _ in range(CALLERS)]
        await asyncio.sleep(5)                                           # a quiet stretch with the 20 callers only
        upload_started = time.perf_counter()
        response = await client.post(f"{base}/org/v1/orgs/{org_id}/invites/csv",
                                     files={"file": ("college_2000.csv", FIXTURE.read_bytes(), "text/csv")},
                                     headers={**bearer(owner, email), "Idempotency-Key": uuid.uuid4().hex})
        upload_finished = time.perf_counter()
        await asyncio.sleep(1)
        stop.set()
        await asyncio.gather(*callers)
    before = [s for done, s in samples if done < upload_started]
    during = [s for done, s in samples if upload_started <= done <= upload_finished]
    body = response.json()
    print(f"upload: HTTP {response.status_code}, {body.get('rows_ok')} invites, {body.get('rows_failed')} errors, "
          f"{upload_finished - upload_started:.2f} s")
    quiet, loud = summarise(before), summarise(during)
    print(line("internal lookups, 20 callers, BEFORE the upload", quiet))
    print(line("internal lookups, 20 callers, DURING the upload", loud))
    print(json.dumps({"before": quiet, "during": loud, "upload_seconds": round(upload_finished - upload_started, 2)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    asyncio.run(main(parser.parse_args().base))
