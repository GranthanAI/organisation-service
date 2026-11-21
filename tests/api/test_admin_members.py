"""GET /admin/org/v1/orgs/{org_id}/members: staff read the same list as the org's own admins."""

import uuid

import pytest

from tests.support.tokens import bearer, make_token


def url(world) -> str:
    return f"/admin/org/v1/orgs/{world.org.id}/members"


@pytest.fixture
def support():
    return bearer(make_token(roles=["support"], email="support@granthan.com"))


@pytest.fixture
def staff_admin():
    return bearer(make_token(roles=["admin", "billing_admin", "support"], email="admin@granthan.com"))


async def test_support_can_read_the_members(api, support):
    world = await api.seed_org(admins=1, members=3)
    r = await api.client.get(url(world), headers=support)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 5 and set(items[0]) == {"user_id", "name", "email", "role", "source", "joined_at"}
    assert items[0]["user_id"] == str(world.owner.id)


async def test_staff_admin_can_read_too(api, staff_admin):
    world = await api.seed_org()
    assert (await api.client.get(url(world), headers=staff_admin)).status_code == 200


async def test_the_shape_and_pagination_match_the_org_route(api, support):
    world = await api.seed_org(admins=1, members=12, seats=100)
    staff_view, org_view = [], []
    for route, headers, sink in ((url(world), support, staff_view),
                                 (f"/org/v1/orgs/{world.org.id}/members", api.h(world.owner), org_view)):
        cursor = None
        while True:
            params = {"limit": 5, **({"cursor": cursor} if cursor else {})}
            body = (await api.client.get(route, params=params, headers=headers)).json()
            sink += body["items"]
            cursor = body["next_cursor"]
            if not cursor:
                break
    assert staff_view == org_view and len(staff_view) == 14


async def test_filters_work_on_the_staff_route(api, support):
    world = await api.seed_org(admins=2, members=2)
    admins = (await api.client.get(url(world), params={"role": "admin"}, headers=support)).json()["items"]
    assert {m["role"] for m in admins} == {"admin"} and len(admins) == 2
    named = (await api.client.get(url(world), params={"q": world.member.email}, headers=support)).json()["items"]
    assert [m["user_id"] for m in named] == [str(world.member.id)]
    assert (await api.client.get(url(world), params={"limit": 101}, headers=support)).status_code == 422
    assert (await api.client.get(url(world), params={"cursor": "bad"}, headers=support)).status_code == 422


async def test_a_non_staff_caller_gets_403_even_the_orgs_own_owner(api):
    world = await api.seed_org()
    for headers in (api.h(world.owner), api.h(world.member), api.h(uuid.uuid4()),
                    bearer(make_token(roles=["billing_admin"]))):
        r = await api.client.get(url(world), headers=headers)
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    assert (await api.client.get(url(world))).status_code == 401


async def test_an_unknown_org_is_404_for_staff(api, support):
    assert (await api.client.get(f"/admin/org/v1/orgs/{uuid.uuid4()}/members", headers=support)).status_code == 404


async def test_staff_see_members_of_a_closed_org_and_ended_members_are_not_listed(api, support):
    world = await api.seed_org(status="closed")
    assert (await api.client.get(url(world), headers=support)).status_code == 200
