"""100 users accept 100 email invites at the same moment, over HTTP, into an organisation with 5 free seats. Exactly 5 must get 200,
95 must get SEATS_FULL (409), and exactly 5 org.member.joined events must be written.

    uv run python -m tests.load.seat_race [--base http://127.0.0.1:8010] [--users 100] [--free 5]

Run it in the background against `make dev`. Exit code 0 only if the counts are exactly right.
"""

import argparse
import asyncio
import collections
import json
import sys
import time
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import func, select

from app.core.clock import system_clock
from app.db.session import unit_of_work
from app.models.invite import Invite
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.known_user_repository import KnownUserRepository
from app.security.tokens import invite_token, invite_token_hash
from app.utils.ids import uuid7
from tests.load.common import bearer, seed_org
from tests.load.stats import line, summarise


async def prepare(users: int, free: int):
    tag = uuid.uuid4().hex[:8]
    org_id, _owner, _ = await seed_org(seats=free + 1, tag=tag)           # the owner uses one seat: `free` are left
    now = system_clock.now()
    people = []
    async with unit_of_work() as s:
        for i in range(users):
            user_id, email = uuid7(), f"racer{i}-{tag}@load.test"
            invite_id = uuid7()
            await KnownUserRepository(s).upsert(user_id, email, f"Racer {i}", now)
            s.add(Invite(id=invite_id, org_id=org_id, kind="email", email=email, role="member", token_hash=invite_token_hash(invite_id, 1),
                         expires_at=now + timedelta(days=30), created_at=now, token_version=1))
            people.append((user_id, email, invite_token(invite_id, 1)))
    return org_id, people


async def main(base: str, users: int, free: int) -> int:
    org_id, people = await prepare(users, free)
    results: list[tuple[int, str | None, float]] = []
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(limits=limits, timeout=60) as client:
        start = asyncio.Event()

        async def accept(user_id, email, tok):
            await start.wait()
            began = time.perf_counter()
            r = await client.post(f"{base}/org/v1/invites/accept", json={"token": tok},
                                  headers={**bearer(user_id, email), "Idempotency-Key": uuid.uuid4().hex})
            code = r.json().get("code") if r.status_code != 200 else None
            results.append((r.status_code, code, time.perf_counter() - began))

        tasks = [asyncio.create_task(accept(*p)) for p in people]
        await asyncio.sleep(0.5)                                         # every task is waiting at the gate
        start.set()
        await asyncio.gather(*tasks)
    counts = collections.Counter((status, code) for status, code, _ in results)
    async with unit_of_work() as s:
        members = (await s.execute(select(func.count()).select_from(Membership).where(
            Membership.org_id == org_id, Membership.status == "active"))).scalar_one()
        events = [e for e in (await s.execute(select(OutboxEvent).where(OutboxEvent.topic == "org.member.joined"))).scalars()
                  if e.payload.get("org_id") == str(org_id)]
    unexpected = sum(1 for status, _, _ in results if status not in (200, 409))
    print(line("POST /org/v1/invites/accept (all at once)", summarise([t for _, _, t in results], unexpected)))
    print(json.dumps({"results": {f"{status} {code or ''}".strip(): n for (status, code), n in counts.items()}, "active_members": members,
                      "joined_events": len(events), "free_seats": free}))
    ok = (counts.get((200, None), 0) == free and counts.get((409, "SEATS_FULL"), 0) == users - free
          and len(events) == free and members == free + 1)
    print(f"PASS: exactly {free} admitted, {users - free} SEATS_FULL, {len(events)} joined events" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--free", type=int, default=5)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.base, args.users, args.free)))
