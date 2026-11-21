"""Rule D1: a user is in at most one active organisation (ux_memberships_one_active_org)."""

import pytest
from sqlalchemy import text

from app.core.enums import EndReason, MembershipStatus
from app.core.exceptions import AlreadyInOrg
from app.db.session import unit_of_work
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.utils.ids import uuid7
from tests.support.factories import NOW, make_membership, make_org


async def two_orgs(session_factory):
    a, b = make_org(), make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(a)
        await OrgRepository(s).add(b)
    return a, b


async def test_second_active_membership_for_the_same_user_raises_already_in_org(session_factory):
    a, b = await two_orgs(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(make_membership(a.id, user))
    with pytest.raises(AlreadyInOrg):
        async with unit_of_work(session_factory) as s:
            await MembershipRepository(s).add(make_membership(b.id, user))


async def test_same_org_twice_is_also_refused(session_factory):
    a, _ = await two_orgs(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(make_membership(a.id, user))
    with pytest.raises(AlreadyInOrg):
        async with unit_of_work(session_factory) as s:
            await MembershipRepository(s).add(make_membership(a.id, user))


async def test_an_ended_membership_plus_a_new_active_one_is_fine(session_factory):
    a, b = await two_orgs(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(make_membership(
            a.id, user, status=MembershipStatus.LEFT.value, ended_at=NOW, end_reason=EndReason.LEFT.value))
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(make_membership(b.id, user))
    async with unit_of_work(session_factory) as s:
        active = await MembershipRepository(s).get_active_for_user(user)
    assert active is not None and active.org_id == b.id


async def test_many_ended_memberships_for_one_user_are_allowed(session_factory):
    a, _ = await two_orgs(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        for _i in range(3):
            await MembershipRepository(s).add(make_membership(
                a.id, user, status=MembershipStatus.REMOVED.value, ended_at=NOW, end_reason=EndReason.REMOVED.value))


async def test_rejoining_after_ending_in_the_same_org_works(session_factory):
    a, _ = await two_orgs(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        repo = MembershipRepository(s)
        old = await repo.add(make_membership(a.id, user))
        old.status, old.ended_at, old.end_reason = "left", NOW, "left"
        await repo.save(old)
        await repo.add(make_membership(a.id, user))
    async with unit_of_work(session_factory) as s:
        assert await MembershipRepository(s).count_active(a.id) == 1


async def test_different_users_can_share_an_org(session_factory):
    a, _ = await two_orgs(session_factory)
    async with unit_of_work(session_factory) as s:
        for _i in range(3):
            await MembershipRepository(s).add(make_membership(a.id))
    async with unit_of_work(session_factory) as s:
        assert await MembershipRepository(s).count_active(a.id) == 3


async def test_the_failed_insert_leaves_the_first_membership_untouched(session_factory):
    a, b = await two_orgs(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        first = await MembershipRepository(s).add(make_membership(a.id, user))
    with pytest.raises(AlreadyInOrg):
        async with unit_of_work(session_factory) as s:
            await MembershipRepository(s).add(make_membership(b.id, user))
    async with unit_of_work(session_factory) as s:
        rows = (await s.execute(text("SELECT id FROM memberships WHERE user_id = :u"), {"u": user})).all()
    assert [r.id for r in rows] == [first.id]
