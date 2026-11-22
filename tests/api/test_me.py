"""GET /org/v1/me: the caller's organisation, role, seats and features."""

import uuid
from datetime import timedelta

import pytest

from app.db.session import unit_of_work
from app.repositories.known_user_repository import KnownUserRepository
from app.services import me_service
from app.telemetry.metrics import INTERNAL_REQUESTS
from tests.support.factories import NOW

ME = "/org/v1/me"


async def test_a_user_in_no_org_gets_404(api):
    r = await api.client.get(ME, headers=api.h(uuid.uuid4()))
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_no_token_is_401_and_a_deleted_user_too(api):
    assert (await api.client.get(ME)).status_code == 401
    world = await api.seed_org()
    async with unit_of_work(api.sf) as s:
        await KnownUserRepository(s).set_deleted(world.member.id, NOW)
    assert (await api.client.get(ME, headers=api.h(world.member))).status_code == 401


@pytest.mark.parametrize("who, role", [("owner", "owner"), ("admin", "admin"), ("member", "member")])
async def test_owner_admin_and_member_views(api, who, role):
    world = await api.seed_org(admins=1, members=3, seats=50, plan_code="org_pro", name="Amity University",
                               pilot_ends_at=None)
    person = getattr(world, who)
    r = await api.client.get(ME, headers=api.h(person))
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == role
    assert body["org"] == {"id": str(world.org.id), "name": "Amity University", "slug": world.org.slug,
                           "type": "college", "status": "active", "pilot_ends_at": None}
    assert body["seats"] == {"plan_code": "org_pro", "status": "active", "used": 5, "bought": 50, "over_by": 0}
    assert set(body) == {"org", "role", "seats", "features"}


async def test_a_pilot_view(api):
    world = await api.seed_org(status="pilot", plan_code="pilot", seats=25, pilot_ends_at=NOW + timedelta(days=12))
    body = (await api.client.get(ME, headers=api.h(world.member))).json()
    assert body["org"]["status"] == "pilot" and body["org"]["pilot_ends_at"].startswith("2026-12-03")
    assert body["seats"] == {"plan_code": "pilot", "status": "active", "used": 5, "bought": 25, "over_by": 0}
    assert body["features"] == {"lock_modes": False, "shared_modes_max": 3, "usage_reports": False, "spend_cap": False,
                                "csv_invites": True, "invite_links": True, "domains": True}


async def test_pay_as_you_go_has_no_bought_count(api):
    world = await api.seed_org(plan_code="payg", seats=None)
    body = (await api.client.get(ME, headers=api.h(world.owner))).json()
    assert body["seats"] == {"plan_code": "payg", "status": "active", "used": 5, "bought": None, "over_by": 0}
    assert body["features"] == {"lock_modes": True, "shared_modes_max": None, "usage_reports": True, "spend_cap": True,
                                "csv_invites": True, "invite_links": True, "domains": True}


@pytest.mark.parametrize("seat_status", ["incomplete", "ended"])
async def test_incomplete_or_ended_seats_report_no_bought_count(api, seat_status):
    world = await api.seed_org(plan_code="org_pro", seats=None, seat_status=seat_status)
    body = (await api.client.get(ME, headers=api.h(world.member))).json()
    assert body["seats"]["bought"] is None and body["seats"]["status"] == seat_status and body["seats"]["over_by"] == 0


async def test_over_by_when_there_are_more_members_than_seats(api):
    world = await api.seed_org(plan_code="org_max", seats=3, members=4)          # 1 owner + 1 admin + 4 members = 6
    seats = (await api.client.get(ME, headers=api.h(world.member))).json()["seats"]
    assert (seats["used"], seats["bought"], seats["over_by"]) == (6, 3, 3)


@pytest.mark.parametrize("plan, expected", [
    ("pilot", {"lock_modes": False, "shared_modes_max": 3, "usage_reports": False, "spend_cap": False}),
    ("org_pro", {"lock_modes": True, "shared_modes_max": 20, "usage_reports": True, "spend_cap": False}),
    ("org_max", {"lock_modes": True, "shared_modes_max": 50, "usage_reports": True, "spend_cap": False}),
    ("payg", {"lock_modes": True, "shared_modes_max": None, "usage_reports": True, "spend_cap": True}),
])
async def test_features_per_plan(api, plan, expected):
    world = await api.seed_org(plan_code=plan, seats=None if plan == "payg" else 60)
    features = (await api.client.get(ME, headers=api.h(world.member))).json()["features"]
    assert {k: features[k] for k in expected} == expected
    assert features["csv_invites"] is features["invite_links"] is features["domains"] is True


async def test_an_org_with_no_seat_row_has_no_paid_features(api):
    world = await api.seed_org(with_seat=False)
    body = (await api.client.get(ME, headers=api.h(world.member))).json()
    assert body["seats"] == {"plan_code": None, "status": None, "used": 5, "bought": None, "over_by": 0}
    assert body["features"]["lock_modes"] is False and body["features"]["usage_reports"] is False


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_reads_work_in_every_status_so_members_can_see_why_access_stopped(api, status):
    world = await api.seed_org(status=status)
    r = await api.client.get(ME, headers=api.h(world.member))
    assert r.status_code == 200 and r.json()["org"]["status"] == status


async def test_an_ended_membership_is_404(api):
    world = await api.seed_org(members=0)
    async with unit_of_work(api.sf) as s:
        gone = await api.person(s, world.org.id, "member", 50, status="left")
    assert (await api.client.get(ME, headers=api.h(gone))).status_code == 404


async def test_the_service_makes_exactly_two_queries(api):
    world = await api.seed_org()

    class Counting:
        def __init__(self, session):
            self.session, self.n = session, 0

        async def execute(self, *a, **k):
            self.n += 1
            return await self.session.execute(*a, **k)

    async with api.sf() as session:
        counting = Counting(session)
        await me_service.get_me(counting, world.member.id)
    assert counting.n == 2                                         # membership + org + seat_state, and one count


async def test_the_timing_histogram_has_a_me_label(api):
    world = await api.seed_org()
    label = INTERNAL_REQUESTS.labels(route="/org/v1/me")
    before = label._sum.get()
    await api.client.get(ME, headers=api.h(world.member))
    assert label._sum.get() > before


async def test_me_shows_past_due_and_cancelling_seats_so_the_admin_knows_why_joins_stop(api):
    for status in ("past_due", "cancelling"):
        world = await api.seed_org(seats=50, seat_status=status)
        seats = (await api.client.get("/org/v1/me", headers=api.h(world.admin))).json()["seats"]
        assert seats["status"] == status and seats["bought"] == 50 and seats["over_by"] == 0         # members stay; the status explains
