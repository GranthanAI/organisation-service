"""GET /org/v1/orgs/{org_id}/audit and the staff view GET /admin/org/v1/orgs/{org_id}/audit."""

import uuid
from datetime import timedelta

import pytest

from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.security.models import CurrentUser
from app.services import audit_service
from tests.support.factories import NOW
from tests.support.tokens import bearer, make_token


def url(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/audit"


def staff_url(world) -> str:
    return f"/admin/org/v1/orgs/{world.org.id}/audit"


async def record(api, world, i, actor, action="member.role_changed", reason=None, details=None, staff=False):
    async with unit_of_work(api.sf) as s:
        await audit_service.record(s, actor, action, "membership", uuid.uuid4(), details or {"i": i}, reason,
                                   org_id=world.org.id, staff=staff, clock=FixedClock(NOW + timedelta(seconds=i)))


def user_actor(person) -> CurrentUser:
    return CurrentUser(id=person.id, email=person.email, jti="j", roles=[])


@pytest.fixture
def staff_headers():
    return bearer(make_token(roles=["admin", "billing_admin", "support"], email="staff@granthan.com"))


async def test_newest_first_with_stable_paging(api):
    world = await api.seed_org()
    for i in range(7):
        await record(api, world, i, user_actor(world.owner))
    first = (await api.client.get(url(world), params={"limit": 3}, headers=api.h(world.owner))).json()
    assert [r["details"]["i"] for r in first["items"]] == [6, 5, 4]
    second = (await api.client.get(url(world), params={"limit": 3, "cursor": first["next_cursor"]},
                                   headers=api.h(world.owner))).json()
    third = (await api.client.get(url(world), params={"limit": 3, "cursor": second["next_cursor"]},
                                  headers=api.h(world.owner))).json()
    assert [r["details"]["i"] for r in second["items"]] == [3, 2, 1] and [r["details"]["i"] for r in third["items"]] == [0]
    assert third["next_cursor"] is None
    seen = [r["id"] for p in (first, second, third) for r in p["items"]]
    assert len(seen) == 7 == len(set(seen))


async def test_a_new_entry_between_pages_does_not_repeat_or_skip(api):
    world = await api.seed_org()
    for i in range(6):
        await record(api, world, i, user_actor(world.owner))
    first = (await api.client.get(url(world), params={"limit": 3}, headers=api.h(world.owner))).json()
    await record(api, world, 99, user_actor(world.owner))                         # newer than everything: before the cursor
    rest = (await api.client.get(url(world), params={"limit": 10, "cursor": first["next_cursor"]},
                                 headers=api.h(world.owner))).json()
    assert [r["details"]["i"] for r in rest["items"]] == [2, 1, 0]


async def test_each_row_has_the_documented_fields(api):
    world = await api.seed_org()
    await record(api, world, 1, user_actor(world.admin), reason="tidying up")
    (row,) = (await api.client.get(url(world), headers=api.h(world.owner))).json()["items"]
    assert set(row) == {"id", "action", "actor_id", "actor_kind", "target_type", "target_id", "details", "reason", "at",
                        "actor_label"}
    assert (row["action"], row["actor_id"], row["actor_kind"], row["target_type"]) == (
        "member.role_changed", str(world.admin.id), "user", "membership")
    assert row["actor_label"] == world.admin.name                       # a user row: the person's name


async def test_filter_by_action_and_by_actor(api):
    world = await api.seed_org()
    await record(api, world, 1, user_actor(world.owner), action="member.role_changed")
    await record(api, world, 2, user_actor(world.admin), action="member.removed")
    await record(api, world, 3, user_actor(world.admin), action="member.role_changed")
    h = api.h(world.owner)
    by_action = (await api.client.get(url(world), params={"action": "member.removed"}, headers=h)).json()["items"]
    assert [r["details"]["i"] for r in by_action] == [2]
    by_actor = (await api.client.get(url(world), params={"actor_id": str(world.admin.id)}, headers=h)).json()["items"]
    assert [r["details"]["i"] for r in by_actor] == [3, 2]
    both = (await api.client.get(url(world), params={"actor_id": str(world.admin.id), "action": "member.role_changed"},
                                 headers=h)).json()["items"]
    assert [r["details"]["i"] for r in both] == [3]
    assert (await api.client.get(url(world), params={"actor_id": "nope"}, headers=h)).status_code == 422


@pytest.mark.parametrize("limit, status", [(1, 200), (100, 200), (101, 422), (0, 422)])
async def test_limit_cap_and_bad_cursor(api, limit, status):
    world = await api.seed_org()
    assert (await api.client.get(url(world), params={"limit": limit}, headers=api.h(world.owner))).status_code == status
    assert (await api.client.get(url(world), params={"cursor": "garbage"}, headers=api.h(world.owner))).status_code == 422


async def test_staff_reasons_are_hidden_from_the_org_and_staff_details_keys_are_dropped(api):
    world = await api.seed_org()
    staff = CurrentUser(id=uuid.uuid4(), email="s@granthan.com", jti="j", roles=["admin"])
    await record(api, world, 1, staff, action="seats.set", reason="sold 50 seats to sales", staff=True,
                 details={"seats": 50, "staff_note": "call back in March"})
    await record(api, world, 2, user_actor(world.admin), reason="the admin said so", details={"staff_x": 1, "keep": 2})
    await record(api, world, 3, "system", action="org.owner_missing", reason="system reason")
    rows = (await api.client.get(url(world), headers=api.h(world.owner))).json()["items"]
    staff_row = next(r for r in rows if r["action"] == "seats.set")
    assert staff_row["actor_kind"] == "staff" and staff_row["reason"] is None
    assert staff_row["actor_id"] is None and staff_row["actor_label"] == "GranthanAI staff"     # M1: who is hidden
    assert staff_row["details"] == {"seats": 50}                                    # staff_note is gone
    user_row = next(r for r in rows if r["actor_kind"] == "user")
    assert user_row["reason"] == "the admin said so"
    assert user_row["details"] == {"keep": 2}                                        # staff_x is gone
    system_row = next(r for r in rows if r["actor_kind"] == "system")
    assert system_row["reason"] is None and system_row["actor_id"] is None and system_row["actor_label"] == "GranthanAI"


async def test_the_staff_route_shows_everything(api, staff_headers):
    world = await api.seed_org()
    staff = CurrentUser(id=uuid.uuid4(), email="s@granthan.com", jti="j", roles=["admin"])
    await record(api, world, 1, staff, action="seats.set", reason="sold 50 seats to sales", staff=True,
                 details={"seats": 50, "staff_note": "call back in March"})
    (row,) = (await api.client.get(staff_url(world), headers=staff_headers)).json()["items"]
    assert row["reason"] == "sold 50 seats to sales" and row["details"] == {"seats": 50, "staff_note": "call back in March"}
    assert row["actor_kind"] == "staff"
    assert row["actor_id"] == str(staff.id) and row["actor_label"] is None            # the staff route is unchanged


async def test_support_can_read_the_staff_route_and_it_pages_and_filters_like_the_other(api):
    world = await api.seed_org()
    for i in range(4):
        await record(api, world, i, user_actor(world.owner))
    support = bearer(make_token(roles=["support"], email="support@granthan.com"))
    first = (await api.client.get(staff_url(world), params={"limit": 3}, headers=support)).json()
    assert len(first["items"]) == 3 and first["next_cursor"]
    rest = (await api.client.get(staff_url(world), params={"cursor": first["next_cursor"]}, headers=support)).json()
    assert len(rest["items"]) == 1
    assert len((await api.client.get(staff_url(world), params={"action": "nothing"}, headers=support)).json()["items"]) == 0


async def test_a_plain_user_cannot_use_the_staff_route_and_an_unknown_org_is_404(api, staff_headers):
    world = await api.seed_org()
    assert (await api.client.get(staff_url(world), headers=api.h(world.owner))).status_code == 403
    assert (await api.client.get(staff_url(world))).status_code == 401
    assert (await api.client.get(f"/admin/org/v1/orgs/{uuid.uuid4()}/audit", headers=staff_headers)).status_code == 404


async def test_owner_and_admin_can_read_a_member_cannot_and_a_stranger_gets_404(api):
    world = await api.seed_org()
    await record(api, world, 1, user_actor(world.owner))
    assert (await api.client.get(url(world), headers=api.h(world.owner))).status_code == 200
    assert (await api.client.get(url(world), headers=api.h(world.admin))).status_code == 200
    r = await api.client.get(url(world), headers=api.h(world.member))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    assert (await api.client.get(url(world), headers=api.h(uuid.uuid4()))).status_code == 404
    other = await api.seed_org()
    assert (await api.client.get(url(world), headers=api.h(other.owner))).status_code == 404
    assert (await api.client.get(url(world))).status_code == 401


async def test_an_org_only_sees_its_own_audit_rows(api):
    a, b = await api.seed_org(), await api.seed_org()
    await record(api, a, 1, user_actor(a.owner))
    await record(api, b, 2, user_actor(b.owner))
    rows = (await api.client.get(url(a), headers=api.h(a.owner))).json()["items"]
    assert [r["details"]["i"] for r in rows] == [1]


async def test_service_rows_are_labelled_granthanai_and_a_user_without_a_name_shows_their_email(api):
    from app.security.models import ServiceCaller
    world = await api.seed_org(admins=0, members=0)
    async with unit_of_work(api.sf) as s:
        nameless = await api.person(s, world.org.id, "member", 5)
        await s.execute(__import__("sqlalchemy").text("UPDATE known_users SET full_name = NULL WHERE user_id = :u"), {"u": nameless.id})
    await record(api, world, 1, ServiceCaller("billing"), action="org.status_changed")
    await record(api, world, 2, user_actor(nameless))
    rows = (await api.client.get(url(world), headers=api.h(world.owner))).json()["items"]
    service_row = next(r for r in rows if r["actor_kind"] == "service")
    assert service_row["actor_id"] is None and service_row["actor_label"] == "GranthanAI"
    assert "service" not in service_row["details"]                                  # which service is not shown either
    user_row = next(r for r in rows if r["actor_kind"] == "user")
    assert user_row["actor_label"] == nameless.email and user_row["actor_id"] == str(nameless.id)


async def test_a_user_who_is_no_longer_known_gets_a_neutral_label(api):
    world = await api.seed_org()
    await record(api, world, 1, user_actor(CurrentUser(id=uuid.uuid4(), email="gone@x.co", jti="j", roles=[])))
    (row,) = (await api.client.get(url(world), headers=api.h(world.owner))).json()["items"]
    assert row["actor_label"] == "A former member"
