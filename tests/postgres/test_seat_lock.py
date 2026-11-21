"""SeatStateRepository.lock_for_update: a second connection waits until the first commits."""

import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import unit_of_work
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from tests.support.factories import make_org, make_seat_state


async def seed(session_factory):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, seats_bought=5))
    return org


async def test_second_lock_waits_until_the_first_commits(session_factory):
    org = await seed(session_factory)
    first_has_lock = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first():
        async with unit_of_work(session_factory) as s:  # connection 1
            assert await SeatStateRepository(s).lock_for_update(org.id) is not None
            order.append("first locked")
            first_has_lock.set()
            await release_first.wait()
            order.append("first committing")
        order.append("first committed")

    async def second():
        await first_has_lock.wait()
        async with unit_of_work(session_factory) as s:  # connection 2
            order.append("second asking")
            row = await SeatStateRepository(s).lock_for_update(org.id)
            order.append("second locked")
            assert row.seats_bought == 5

    t1, t2 = asyncio.create_task(first()), asyncio.create_task(second())
    await first_has_lock.wait()
    await asyncio.sleep(0.5)
    assert not t2.done(), "the second lock must block while the first transaction is open"
    assert "second locked" not in order
    release_first.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=10)
    assert order.index("first committing") < order.index("second locked")


async def test_the_second_lock_times_out_while_the_first_is_held(session_factory):
    org = await seed(session_factory)
    async with unit_of_work(session_factory) as s1:
        await SeatStateRepository(s1).lock_for_update(org.id)
        async with session_factory() as s2:
            await s2.execute(text("SET LOCAL lock_timeout = '300ms'"))
            started = time.monotonic()
            with pytest.raises(DBAPIError, match="lock timeout"):
                await SeatStateRepository(s2).lock_for_update(org.id)
            assert 0.25 <= time.monotonic() - started < 3  # it waited, then gave up
    # after the first transaction ended, a fresh lock is immediate
    async with unit_of_work(session_factory) as s3:
        row = await asyncio.wait_for(SeatStateRepository(s3).lock_for_update(org.id), timeout=5)
        assert row is not None


async def test_locking_a_missing_org_returns_none(session_factory):
    from app.utils.ids import uuid7
    async with unit_of_work(session_factory) as s:
        assert await SeatStateRepository(s).lock_for_update(uuid7()) is None


async def test_locks_on_different_orgs_do_not_block_each_other(session_factory):
    a, b = await seed(session_factory), await seed(session_factory)
    async with unit_of_work(session_factory) as s1:
        await SeatStateRepository(s1).lock_for_update(a.id)
        async with unit_of_work(session_factory) as s2:
            row = await asyncio.wait_for(SeatStateRepository(s2).lock_for_update(b.id), timeout=5)
            assert row is not None
