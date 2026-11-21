"""20 concurrent joins (separate sessions, separate connections) into 5 free seats admit exactly 5."""

import asyncio
import uuid
from collections import Counter

from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.enums import JoinSource
from app.core.exceptions import AlreadyInOrg, DomainError, PilotFull, SeatsFull
from app.db.session import unit_of_work
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import join_service
from tests.support.factories import NOW, ensure_known, make_membership, make_org, make_seat_state


async def attempt(session_factory, org_id, user_id):
    clock = FixedClock(NOW)
    await ensure_known(session_factory, user_id)
    try:
        async with unit_of_work(session_factory) as s:
            await join_service.join(s, org_id, user_id, JoinSource.LINK, actor="system", clock=clock)
        return "joined"
    except DomainError as exc:
        return exc.code


async def seed(session_factory, seats, members, **org_kw):
    org = make_org(**org_kw)
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, seats_bought=seats))
        for _ in range(members):
            await MembershipRepository(s).add(make_membership(org.id))
    return org


async def test_twenty_concurrent_joins_into_five_free_seats_admit_exactly_five(session_factory):
    org = await seed(session_factory, seats=8, members=3)                 # 5 seats free
    results = await asyncio.gather(*[attempt(session_factory, org.id, uuid.uuid4()) for _ in range(20)])
    assert Counter(results) == {"joined": 5, "SEATS_FULL": 15}

    async with session_factory() as s:
        members = list((await s.execute(select(Membership).where(Membership.status == "active"))).scalars())
        events = list((await s.execute(select(OutboxEvent))).scalars())
    assert len(members) == 8                                                # never over the seats
    assert len(events) == 5 and {e.topic for e in events} == {"org.member.joined"}
    assert len({e.key for e in events}) == 5


async def test_the_pilot_cap_holds_under_concurrency(session_factory):
    org = await seed(session_factory, seats=None, members=22, status="pilot")      # 3 free below the cap of 25
    results = await asyncio.gather(*[attempt(session_factory, org.id, uuid.uuid4()) for _ in range(12)])
    assert Counter(results) == {"joined": 3, "PILOT_FULL": 9}
    async with session_factory() as s:
        assert len(list((await s.execute(select(Membership))).scalars())) == 25


async def test_the_same_user_joining_twice_at_once_gets_exactly_one_membership(session_factory):
    org = await seed(session_factory, seats=50, members=0)
    user = uuid.uuid4()
    results = await asyncio.gather(*[attempt(session_factory, org.id, user) for _ in range(8)])
    assert Counter(results) == {"joined": 1, "ALREADY_IN_ORG": 7}
    async with session_factory() as s:
        assert len(list((await s.execute(select(Membership).where(Membership.user_id == user))).scalars())) == 1


async def test_a_join_and_a_leave_at_the_same_time_never_oversell(session_factory):
    from app.core.enums import EndReason
    from app.services import membership_service
    org = await seed(session_factory, seats=3, members=3)                  # full
    async with session_factory() as s:
        leaver = (await s.execute(select(Membership).limit(1))).scalar_one().user_id

    async def leave():
        async with unit_of_work(session_factory) as s:
            await membership_service.end(s, org.id, leaver, EndReason.LEFT, "system", clock=FixedClock(NOW))
        return "left"

    results = await asyncio.gather(leave(), *[attempt(session_factory, org.id, uuid.uuid4()) for _ in range(6)])
    assert results.count("left") == 1 and results.count("joined") <= 1       # at most the one freed seat
    assert set(results) <= {"left", "joined", "SEATS_FULL"}
    async with session_factory() as s:
        active = list((await s.execute(select(Membership).where(Membership.status == "active"))).scalars())
    assert len(active) <= 3


def test_the_error_classes_used_here_are_the_ones_from_g7():
    assert (SeatsFull.code, PilotFull.code, AlreadyInOrg.code) == ("SEATS_FULL", "PILOT_FULL", "ALREADY_IN_ORG")
