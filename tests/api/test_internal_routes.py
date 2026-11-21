"""The /internal routes billing-service calls: shapes field by field, 404s, the K4 status rule, the alias, the members cap,
auth, and billing's own (copied) parser reading every response."""

import uuid
from datetime import timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from app.api.deps import get_db_session
from app.db.session import unit_of_work
from app.main import app
from app.models.membership import Membership
from app.models.policy import OrgPolicy
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.telemetry.metrics import INTERNAL_REQUESTS
from tests.support.billing_org_client import HttpOrgDirectory, OrgDirectoryUnavailable, OrgInfo, OrgVariant
from tests.support.billing_org_client import Membership as BillingMembership
from tests.support.factories import NOW, make_membership, make_org
from tests.support.tokens import make_token

TOKEN = "dev-billing-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def env(session_factory):
    async def session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://org", headers=AUTH) as client:
        billing = HttpOrgDirectory("http://org", TOKEN, transport=transport)
        yield client, billing, session_factory
        await billing.aclose()
    app.dependency_overrides.clear()


async def seed_member(session_factory, org_status="active", org_type="college", role="member", **org_kw):
    org = make_org(status=org_status, type=org_type, **org_kw)
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await MembershipRepository(s).add(make_membership(org.id, user, role=role))
    return org, user


# ---------------------------------------------------------------- /internal/memberships/{user_id}
async def test_membership_shape_field_by_field(env):
    client, billing, sf = env
    org, user = await seed_member(sf, role="admin", org_type="company")
    r = await client.get(f"/internal/memberships/{user}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    assert r.json() == {"org_id": str(org.id), "org_type": "company", "role": "admin", "status": "active"}


async def test_the_alias_route_returns_the_same_body(env):
    client, _, sf = env
    _, user = await seed_member(sf)
    a = await client.get(f"/internal/memberships/{user}")
    b = await client.get(f"/internal/users/{user}/membership")
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert (await client.get(f"/internal/users/{uuid.uuid4()}/membership")).status_code == 404


async def test_no_membership_is_404(env):
    client, billing, _ = env
    r = await client.get(f"/internal/memberships/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert await billing.membership(uuid.uuid4()) is None                          # billing reads any 404 as "no org"


async def test_an_ended_membership_is_404(env):
    from app.core.enums import EndReason
    from app.services import membership_service
    client, _, sf = env
    org, user = await seed_member(sf)
    async with unit_of_work(sf) as s:
        await membership_service.end(s, org.id, user, EndReason.LEFT, "system")
    assert (await client.get(f"/internal/memberships/{user}")).status_code == 404


@pytest.mark.parametrize("org_status, reported", [
    ("pilot", "active"), ("active", "active"),                    # these grant the org plan
    ("suspended", "suspended"), ("pilot_expired", "pilot_expired"), ("closed", "closed"),   # these do not (K4)
])
async def test_k4_the_status_billing_reads_follows_the_org(env, org_status, reported):
    client, billing, sf = env
    org, user = await seed_member(sf, org_status=org_status)
    assert (await client.get(f"/internal/memberships/{user}")).json()["status"] == reported
    parsed = await billing.membership(user)
    assert parsed.status == reported
    assert (parsed.status == "active") == (org_status in ("pilot", "active"))      # billing grants only for "active"


async def test_suspending_then_reactivating_flips_the_reported_status_back(env):
    client, _, sf = env
    org, user = await seed_member(sf)
    for status, expected in (("suspended", "suspended"), ("active", "active")):
        async with unit_of_work(sf) as s:
            row = await OrgRepository(s).get(org.id)
            row.status = status
            await OrgRepository(s).save(row)
        assert (await client.get(f"/internal/memberships/{user}")).json()["status"] == expected


async def test_no_4xx_other_than_404_for_a_normal_lookup(env):
    client, _, sf = env
    _, user = await seed_member(sf)
    for path in (f"/internal/memberships/{user}", f"/internal/memberships/{uuid.uuid4()}",
                 f"/internal/orgs/{uuid.uuid4()}", f"/internal/orgs/{uuid.uuid4()}/members"):
        assert (await client.get(path)).status_code in (200, 404)


# ---------------------------------------------------------------- /internal/orgs/{org_id}
async def test_org_shape_field_by_field(env):
    client, billing, sf = env
    org, _ = await seed_member(sf, name="Amity University", gstin="09AAAAA0000A1Z5", state_code="09",
                               billing_email="Accounts@Amity.edu", legal_name="Amity Education Ltd")
    async with unit_of_work(sf) as s:
        s.add(OrgPolicy(org_id=org.id, member_spend_cap_paise=50_000, version=1, updated_at=NOW))
    r = await client.get(f"/internal/orgs/{org.id}")
    assert r.status_code == 200
    assert r.json() == {"org_id": str(org.id), "name": "Amity University", "type": "college", "gstin": "09AAAAA0000A1Z5",
                        "billing_email": "Accounts@Amity.edu", "state_code": "09", "member_spend_cap_paise": 50_000}
    assert await billing.org(org.id) == OrgInfo(org.id, "Amity University", OrgVariant.COLLEGE, "09AAAAA0000A1Z5",
                                                "Accounts@Amity.edu", "09", 50_000)


async def test_org_nulls_when_there_is_no_policy_gstin_or_state(env):
    client, billing, sf = env
    org, _ = await seed_member(sf, org_type="company")
    body = (await client.get(f"/internal/orgs/{org.id}")).json()
    assert body["gstin"] is None and body["state_code"] is None and body["billing_email"] is None
    assert body["member_spend_cap_paise"] is None and body["type"] == "company"
    parsed = await billing.org(org.id)
    assert parsed.gstin is None and parsed.state_code is None and parsed.member_spend_cap_paise is None


async def test_a_policy_row_without_a_cap_is_a_null_cap(env):
    client, _, sf = env
    org, _ = await seed_member(sf)
    async with unit_of_work(sf) as s:
        s.add(OrgPolicy(org_id=org.id, member_spend_cap_paise=None, version=1, updated_at=NOW))
    assert (await client.get(f"/internal/orgs/{org.id}")).json()["member_spend_cap_paise"] is None


async def test_a_closed_org_is_still_returned_and_an_unknown_one_is_404(env):
    client, billing, sf = env
    org, _ = await seed_member(sf, org_status="closed")
    assert (await client.get(f"/internal/orgs/{org.id}")).status_code == 200
    assert (await client.get(f"/internal/orgs/{uuid.uuid4()}")).status_code == 404
    assert await billing.org(uuid.uuid4()) is None


# ---------------------------------------------------------------- /internal/orgs/{org_id}/members
async def test_members_are_active_only_and_ordered_by_joined_at(env):
    client, billing, sf = env
    org = make_org()
    users = [uuid.uuid4() for _ in range(4)]
    async with unit_of_work(sf) as s:
        await OrgRepository(s).add(org)
        repo = MembershipRepository(s)
        await repo.add(make_membership(org.id, users[2], joined_at=NOW + timedelta(minutes=2)))
        await repo.add(make_membership(org.id, users[0], joined_at=NOW))
        await repo.add(make_membership(org.id, users[1], joined_at=NOW + timedelta(minutes=1)))
        await repo.add(make_membership(org.id, users[3], status="left", ended_at=NOW, end_reason="left"))
    r = await client.get(f"/internal/orgs/{org.id}/members")
    assert r.status_code == 200 and set(r.json()) == {"user_ids"}
    assert r.json()["user_ids"] == [str(u) for u in users[:3]]
    assert await billing.members(org.id) == users[:3]


async def test_an_org_with_no_members_is_an_empty_list_and_an_unknown_org_is_404(env):
    client, billing, sf = env
    org = make_org()
    async with unit_of_work(sf) as s:
        await OrgRepository(s).add(org)
    assert (await client.get(f"/internal/orgs/{org.id}/members")).json() == {"user_ids": []}
    assert (await client.get(f"/internal/orgs/{uuid.uuid4()}/members")).status_code == 404
    assert await billing.members(uuid.uuid4()) == []                                # billing: 404 gives []


async def bulk_members(sf, org, count):
    async with unit_of_work(sf) as s:
        await OrgRepository(s).add(org)
        rows = [{"id": uuid.uuid4(), "org_id": org.id, "user_id": uuid.uuid4(), "role": "member", "status": "active",
                 "source": "staff", "joined_at": NOW + timedelta(seconds=i)} for i in range(count)]
        await s.execute(insert(Membership), rows)


async def test_exactly_10000_members_are_returned(env):
    client, billing, sf = env
    org = make_org()
    await bulk_members(sf, org, 10_000)
    r = await client.get(f"/internal/orgs/{org.id}/members")
    assert r.status_code == 200 and len(r.json()["user_ids"]) == 10_000
    assert len(await billing.members(org.id)) == 10_000


async def test_above_10000_members_is_422_and_logged(env, caplog):
    client, billing, sf = env
    org = make_org()
    await bulk_members(sf, org, 10_001)
    r = await client.get(f"/internal/orgs/{org.id}/members")
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    assert "over the limit" in caplog.text and str(org.id) in caplog.text
    with pytest.raises(OrgDirectoryUnavailable):                                       # billing raises on a non-404 4xx
        await billing.members(org.id)


# ---------------------------------------------------------------- auth
@pytest.mark.parametrize("path", [
    "/internal/memberships/{u}", "/internal/users/{u}/membership", "/internal/orgs/{u}", "/internal/orgs/{u}/members",
])
async def test_every_route_needs_a_service_token(env, path):
    client, _, _ = env
    url = path.format(u=uuid.uuid4())
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {make_token()}"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://org") as anon:
            r = await anon.get(url, headers=headers)
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"


async def test_the_other_service_token_works_too(env):
    client, _, sf = env
    _, user = await seed_member(sf)
    r = await client.get(f"/internal/memberships/{user}", headers={"Authorization": "Bearer dev-gateway-token"})
    assert r.status_code == 200


# ---------------------------------------------------------------- the copied billing parser
async def test_billings_parser_reads_every_response(env):
    client, billing, sf = env
    org, user = await seed_member(sf, name="Acme", org_type="company", state_code="27", gstin="27AAAAA0000A1Z5",
                                  billing_email="a@acme.co", role="owner")
    m = await billing.membership(user)
    assert m == BillingMembership(org.id, OrgVariant.COMPANY, "owner", "active")
    o = await billing.org(org.id)
    assert (o.org_id, o.name, o.type, o.gstin, o.billing_email, o.state_code) == (
        org.id, "Acme", OrgVariant.COMPANY, "27AAAAA0000A1Z5", "a@acme.co", "27")
    assert await billing.members(org.id) == [user]
    assert await billing.membership(uuid.uuid4()) is None and await billing.org(uuid.uuid4()) is None


async def test_the_internal_timing_histogram_is_labelled_by_route(env):
    client, _, sf = env
    _, user = await seed_member(sf)
    sample = lambda: INTERNAL_REQUESTS.labels(route="/internal/memberships/{user_id}")._sum.get()  # noqa: E731
    before = sample()
    await client.get(f"/internal/memberships/{user}")
    assert sample() > before
    assert isinstance(httpx.__version__, str)
