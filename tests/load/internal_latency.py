"""GET /internal/memberships/{id}: 2,000 calls at concurrency 1 and at 20 against a RUNNING service, over real HTTP.

    uv run python -m tests.load.internal_latency [--base http://127.0.0.1:8010] [--n 2000] [--user <member id>]

Without --user it seeds one organisation in the dev database (scripts.seed_dev) and uses its member. Prints p50, p95, p99
and the error count. Run it in the background; it only calls this service.
"""

import argparse
import asyncio
import json
import time

import httpx

from tests.load.stats import line, summarise

TOKEN = {"Authorization": "Bearer dev-billing-token"}


async def one(client: httpx.AsyncClient, url: str, out: list[float], errors: list[int]) -> None:
    started = time.perf_counter()
    try:
        r = await client.get(url, headers=TOKEN)
        ok = r.status_code == 200
    except httpx.HTTPError:
        ok = False
    out.append(time.perf_counter() - started)
    if not ok:
        errors.append(1)


async def run(base: str, user: str, n: int, concurrency: int, path: str | None = None) -> dict:
    url = f"{base}{path}" if path else f"{base}/internal/memberships/{user}"
    out: list[float] = []
    errors: list[int] = []
    limits = httpx.Limits(max_connections=max(concurrency, 1), max_keepalive_connections=max(concurrency, 1))
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        for _ in range(50):
            await one(client, url, [], [])                          # warm up (connections, caches)
        gate = asyncio.Semaphore(concurrency)

        async def bounded() -> None:
            async with gate:
                await one(client, url, out, errors)

        await asyncio.gather(*[bounded() for _ in range(n)])
    return summarise(out, len(errors))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--n", type=int, default=2000)
    parser.add_argument("--user", default=None)
    args = parser.parse_args()
    user = args.user
    if user is None:
        from scripts import seed_dev
        user = (await seed_dev.main())["member_id"]
    results = {}
    for concurrency in (1, 20):                                      # the floor: a route that touches no database at all
        floor = await run(args.base, user, args.n, concurrency, "/internal/ping")
        results[f"ping c={concurrency}"] = floor
        print(line(f"GET /internal/ping (no database) c={concurrency}", floor))
    for concurrency in (1, 20):
        results[f"c={concurrency}"] = await run(args.base, user, args.n, concurrency)
        print(line(f"GET /internal/memberships/<id> c={concurrency}", results[f"c={concurrency}"]))
    print(json.dumps(results))


if __name__ == "__main__":
    asyncio.run(main())
