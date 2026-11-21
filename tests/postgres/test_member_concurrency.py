"""Concurrent member operations: no deadlock (lock order is seat_state, then memberships by id) and a consistent result."""

import asyncio
import uuid
from collections import Counter

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.enums import Role
from app.core.exceptions import DomainError
from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser, OrgContext
from app.services import membership_service, org_member_service
from app.services.permission_rules import Action
from tests.support.factories import NOW, make_membership, make_org, make_seat_state

CLOCK = FixedClock(NOW)
TIMEOUT = 30


def ctx_for(org, user_id, role: Role, action: Action) -> OrgContext:
    user = CurrentUser(id=user_id, email=f"{user_id.hex[:8]}@amity.edu", jti="j")
    return OrgContext(org_id=org.id, user=user, role=role, membership_id=uuid.uuid4(), action=action)


async def seed(session_factory, owners=1, admins=2, members=3):
    org = make_org()
    people: dict[str, list[uuid.UUID]] = {"owner": [], "admin": [], "member": []}
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, seats_bought=100))
        for role, count in (("owner", owners), ("admin", admins), ("member", members)):
            for _ in range(count):
                user_id = uuid.uuid4()
                people[role].append(user_id)
                await MembershipRepository(s).add(make_membership(org.id, user_id, role=role))
    return org, people


async def run(session_factory, coro_factory):
    """Run the use case in its own transaction; return 'ok' or the domain error code."""
    try:
        async with unit_of_work(session_factory) as s:
            await coro_factory(s)
        return "ok"
    except DomainError as exc:
        return exc.code


async def roles(session_factory, org) -> dict[uuid.UUID, tuple[str, str]]:
    async with session_factory() as s:
        return {m.user_id: (m.role, m.status)
                for m in (await s.execute(select(Membership).where(Membership.org_id == org.id))).scalars()}


async def test_two_admins_removing_the_same_member_at_once_give_one_success_and_one_404(session_factory):
    org, p = await seed(session_factory)
    target = p["member"][0]
    a1, a2 = p["admin"]

    async def remove_by(admin_id):
        ctx = ctx_for(org, admin_id, Role.ADMIN, Action.REMOVE_MEMBER)
        return await run(session_factory, lambda s: org_member_service.remove_member(s, ctx, target, CLOCK))

    results = await asyncio.wait_for(asyncio.gather(remove_by(a1), remove_by(a2)), TIMEOUT)
    assert sorted(results) == ["NOT_FOUND", "ok"]                                  # one 204, one 404
    async with session_factory() as s:
        left = [e for e in (await s.execute(select(OutboxEvent))).scalars() if e.topic == "org.member.left"]
        removed = [a for a in (await s.execute(select(AuditLog))).scalars() if a.action == "member.removed"]
    assert len(left) == 1 and len(removed) == 1                                    # emitted and audited once
    assert (await roles(session_factory, org))[target] == ("member", "removed")


@pytest.mark.parametrize("round_", range(5))
async def test_transfer_and_remove_at_once_never_deadlock_and_stay_consistent(session_factory, round_):
    """The owner transfers to admin A while also removing member M (and, in another run, removing A itself)."""
    org, p = await seed(session_factory, admins=1)
    owner, admin, member = p["owner"][0], p["admin"][0], p["member"][0]
    ctx = ctx_for(org, owner, Role.OWNER, Action.TRANSFER_OWNERSHIP)
    ctx_remove = ctx_for(org, owner, Role.OWNER, Action.REMOVE_MEMBER)

    results = await asyncio.wait_for(asyncio.gather(
        run(session_factory, lambda s: membership_service.transfer_ownership(s, org.id, admin, ctx, clock=CLOCK)),
        run(session_factory, lambda s: org_member_service.remove_member(s, ctx_remove, member, CLOCK)),
    ), TIMEOUT)
    assert results == ["ok", "ok"]                                                  # no deadlock, both complete
    state = await roles(session_factory, org)
    assert state[admin] == ("owner", "active") and state[owner] == ("admin", "active")
    assert state[member] == ("member", "removed")
    assert sum(1 for role, status in state.values() if role == "owner" and status == "active") == 1


@pytest.mark.parametrize("round_", range(5))
async def test_transfer_to_an_admin_while_the_old_owner_removes_that_admin(session_factory, round_):
    """Both orders are legal, but never an inconsistent state: either the admin was removed before the transfer (so the
    transfer fails with a 422) or the transfer won and the old owner, now an admin, may not remove the new owner (403)."""
    org, p = await seed(session_factory, admins=1)
    owner, admin = p["owner"][0], p["admin"][0]
    t_ctx = ctx_for(org, owner, Role.OWNER, Action.TRANSFER_OWNERSHIP)
    r_ctx = ctx_for(org, owner, Role.OWNER, Action.REMOVE_MEMBER)       # the request carried a (possibly stale) owner role

    results = await asyncio.wait_for(asyncio.gather(
        run(session_factory, lambda s: membership_service.transfer_ownership(s, org.id, admin, t_ctx, clock=CLOCK)),
        run(session_factory, lambda s: org_member_service.remove_member(s, r_ctx, admin, CLOCK)),
    ), TIMEOUT)
    transfer, remove = results
    state = await roles(session_factory, org)
    active_owners = [u for u, (role, status) in state.items() if role == "owner" and status == "active"]
    assert len(active_owners) == 1                                                  # never zero, never two
    if transfer == "ok":
        assert remove == "FORBIDDEN_ROLE" and state[admin] == ("owner", "active")   # the stale owner role was not trusted
    else:
        assert transfer == "VALIDATION_ERROR" and remove == "ok" and state[admin][1] == "removed"
        assert state[owner] == ("owner", "active")


async def test_change_role_and_transfer_at_once_do_not_deadlock(session_factory):
    org, p = await seed(session_factory, admins=2)
    owner, a1, a2 = p["owner"][0], *p["admin"]
    t_ctx = ctx_for(org, owner, Role.OWNER, Action.TRANSFER_OWNERSHIP)
    c_ctx = ctx_for(org, owner, Role.OWNER, Action.CHANGE_ROLE)
    results = await asyncio.wait_for(asyncio.gather(
        run(session_factory, lambda s: membership_service.transfer_ownership(s, org.id, a1, t_ctx, clock=CLOCK)),
        run(session_factory, lambda s: membership_service.change_role(s, org.id, a2, Role.MEMBER, c_ctx, clock=CLOCK)),
    ), TIMEOUT)
    assert results[0] == "ok"
    assert results[1] in ("ok", "FORBIDDEN_ROLE")          # if the transfer went first the old owner may no longer change roles
    state = await roles(session_factory, org)
    assert state[a1] == ("owner", "active") and state[owner] == ("admin", "active")


async def test_many_leaves_and_removals_at_once_end_each_member_exactly_once(session_factory):
    org, p = await seed(session_factory, admins=1, members=12)
    owner, admin = p["owner"][0], p["admin"][0]
    jobs = []
    for i, m in enumerate(p["member"]):
        if i % 2 == 0:
            ctx = ctx_for(org, m, Role.MEMBER, Action.LEAVE)
            jobs.append(run(session_factory, lambda s, c=ctx: org_member_service.leave(s, c, CLOCK)))
        else:
            ctx = ctx_for(org, admin, Role.ADMIN, Action.REMOVE_MEMBER)
            jobs.append(run(session_factory, lambda s, c=ctx, t=m: org_member_service.remove_member(s, c, t, CLOCK)))
        # and the same member is hit a second time from the other side
        other = ctx_for(org, owner, Role.OWNER, Action.REMOVE_MEMBER)
        jobs.append(run(session_factory, lambda s, c=other, t=m: org_member_service.remove_member(s, c, t, CLOCK)))
    results = await asyncio.wait_for(asyncio.gather(*jobs), TIMEOUT)
    assert Counter(results)["ok"] == 12 and set(results) <= {"ok", "NOT_FOUND"}     # each member ended exactly once
    state = await roles(session_factory, org)
    assert all(state[m][1] != "active" for m in p["member"])
    async with session_factory() as s:
        left = [e for e in (await s.execute(select(OutboxEvent))).scalars() if e.topic == "org.member.left"]
    assert len(left) == 12 == len({e.key for e in left})


async def test_the_lock_order_is_seat_state_first(session_factory):
    """While one transaction holds the seat_state row, a second use case waits on it before touching any membership."""
    org, p = await seed(session_factory)
    holder_has_lock, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with unit_of_work(session_factory) as s:
            await membership_service.lock_org(s, org.id)
            holder_has_lock.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await holder_has_lock.wait()
    ctx = ctx_for(org, p["owner"][0], Role.OWNER, Action.REMOVE_MEMBER)
    waiting = asyncio.create_task(run(session_factory, lambda s: org_member_service.remove_member(
        s, ctx, p["member"][0], CLOCK)))
    await asyncio.sleep(0.5)
    assert not waiting.done()                                                       # blocked on seat_state
    async with session_factory() as s:                                              # and no membership row is locked yet
        locked = (await s.execute(select(Membership).where(Membership.user_id == p["member"][0]).with_for_update(
            nowait=True))).scalar_one()
        assert locked.status == "active"
        await s.rollback()
    release.set()
    assert await asyncio.wait_for(waiting, TIMEOUT) == "ok"
    await task
