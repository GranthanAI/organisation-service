"""GET /org/v1/orgs/{id}/usage: the report built from billing's answer (a fake billing over an httpx mock transport)."""

import csv
import io
import json
import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from app.clients.billing_client import BillingClient
from app.db.session import unit_of_work
from app.repositories.known_user_repository import KnownUserRepository
from app.services import usage_service
from app.telemetry.metrics import BILLING_REQUESTS
from tests.support.factories import NOW

FIXTURE = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "billing_org_usage.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 21)                       # NOW (the FixedClock) is 21 Nov 2026 in India too


class FakeBilling:
    """Records every call and answers from `behaviour` (or with `rows`); installed as the usage service's billing client."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.rows: list[dict] = []
        self.behaviour = None                     # a callable(request) -> httpx.Response, or one that raises

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.behaviour is not None:
            return self.behaviour(request)
        return httpx.Response(200, json={"org_id": request.url.path.split("/")[3], "from": request.url.params["from"],
                                         "to": request.url.params["to"], "rows": self.rows})

    def client(self) -> BillingClient:
        return BillingClient("http://billing.test", "test-token", httpx.MockTransport(self.handler))


@pytest.fixture
def billing(monkeypatch):
    fake = FakeBilling()
    monkeypatch.setattr(usage_service, "get_billing_client", fake.client)
    return fake


def row(user, group="fast", messages=1, units=None, day="2026-11-02"):
    return {"user_id": str(user), "day": day, "model_group": group, "messages": messages,
            "billable_units": messages if units is None else units}


def url(world, start="2026-11-01", end="2026-11-07", fmt=None) -> str:
    return f"/org/v1/orgs/{world.org.id}/usage?from={start}&to={end}" + (f"&format={fmt}" if fmt else "")


async def get(api, world, who=None, **kw):
    return await api.client.get(url(world, **kw), headers=api.h(who or world.admin))


# ---------------------------------------------------------------- the contract
def test_the_contract_fixture_is_billings_real_shape():
    body = FIXTURE["response"]
    assert set(body) == {"org_id", "from", "to", "rows"}
    assert set(body["rows"][0]) == {"user_id", "day", "model_group", "messages", "billable_units"}
    assert {r["model_group"] for r in body["rows"]} <= {"fast", "smart", "best", "ultra"}


async def test_the_report_is_built_from_the_real_shaped_answer_and_billing_gets_the_right_request(api, billing):
    world = await api.seed_org(plan_code="org_pro")
    a, b = FIXTURE["response"]["rows"][0]["user_id"], FIXTURE["response"]["rows"][3]["user_id"]
    async with unit_of_work(api.sf) as s:
        await KnownUserRepository(s).upsert(uuid.UUID(a), "ada@amity.edu", "Ada", NOW)
        await KnownUserRepository(s).upsert(uuid.UUID(b), "ben@amity.edu", "Ben", NOW)
    billing.rows = FIXTURE["response"]["rows"]
    r = await get(api, world)
    assert r.status_code == 200
    body = r.json()
    assert body["from"] == "2026-11-01" and body["to"] == "2026-11-07"
    assert body["totals"] == {"messages": 63, "billable_units": 229, "by_model_group": {
        "best": {"messages": 40, "billable_units": 200}, "fast": {"messages": 20, "billable_units": 20},
        "smart": {"messages": 3, "billable_units": 9}}}
    assert [(m["name"], m["messages"]) for m in body["members"]] == [("Ben", 40), ("Ada", 23)]      # most messages first
    ada = body["members"][1]
    assert ada["by_model_group"] == {"fast": {"messages": 20, "billable_units": 20}, "smart": {"messages": 3, "billable_units": 9}}
    assert (ada["billable_units"], ada["email"], ada["role"]) == (29, "ada@amity.edu", None)       # not in this org: role null
    (call,) = billing.calls
    assert call.url.path == f"/internal/orgs/{world.org.id}/usage" and dict(call.url.params) == {"from": "2026-11-01", "to": "2026-11-07"}
    assert call.headers["authorization"] == "Bearer test-token" and call.method == "GET"


# ---------------------------------------------------------------- names, roles, people who left, deleted users
async def test_members_get_name_email_and_current_role_and_the_rest_are_handled(api, billing):
    world = await api.seed_org(plan_code="org_pro", admins=1, members=2)
    left, deleted, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with unit_of_work(api.sf) as s:
        users = KnownUserRepository(s)
        await users.upsert(left, "left@amity.edu", "Left Student", NOW)
        await users.upsert(deleted, "gone@amity.edu", "Gone Person", NOW)
        await users.mark_deleted(deleted, NOW)
    billing.rows = [row(world.owner.id, messages=50), row(world.admin.id, messages=30), row(world.members[0].id, messages=10),
                    row(left, messages=5), row(deleted, messages=7), row(stranger, messages=1)]
    body = (await get(api, world)).json()
    members = {m["user_id"]: m for m in body["members"]}
    assert (members[str(world.owner.id)]["role"], members[str(world.owner.id)]["name"]) == ("owner", world.owner.name)
    assert members[str(world.admin.id)]["role"] == "admin" and members[str(world.members[0].id)]["email"] == world.members[0].email
    assert members[str(left)]["role"] is None and members[str(left)]["name"] == "Left Student"       # no longer a member
    assert members[str(deleted)]["name"] == "Deleted user" and members[str(deleted)]["email"] is None
    assert "gone@amity.edu" not in json.dumps(body) and "Gone Person" not in json.dumps(body)
    assert members[str(stranger)]["name"] is None and members[str(stranger)]["email"] is None
    assert [m["messages"] for m in body["members"]] == [50, 30, 10, 7, 5, 1]


async def test_an_empty_report(api, billing):
    world = await api.seed_org(plan_code="org_pro")
    body = (await get(api, world)).json()
    assert body["members"] == [] and body["totals"] == {"messages": 0, "billable_units": 0, "by_model_group": {}}


# ---------------------------------------------------------------- CSV
async def test_the_csv_is_one_row_per_member_and_model_group_with_a_bom_and_a_filename(api, billing):
    world = await api.seed_org(plan_code="org_pro", name="Amity University")
    user = uuid.uuid4()
    async with unit_of_work(api.sf) as s:
        await KnownUserRepository(s).upsert(user, "ada@amity.edu", "Ada", NOW)
    billing.rows = [row(user, "fast", 20, 20), row(user, "smart", 3, 9), row(world.owner.id, "fast", 5, 5)]
    r = await get(api, world, fmt="csv")
    assert r.status_code == 200 and r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == f'attachment; filename="usage-{world.org.slug}-2026-11-01-2026-11-07.csv"'
    assert r.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["user_id", "name", "email", "role", "model_group", "messages", "billable_units"]
    assert rows[1:] == [[str(user), "Ada", "ada@amity.edu", "", "fast", "20", "20"], [str(user), "Ada", "ada@amity.edu", "", "smart", "3", "9"],
                        [str(world.owner.id), world.owner.name, world.owner.email, "owner", "fast", "5", "5"]]


@pytest.mark.parametrize("name", ['=HYPERLINK("http://evil")', "+1+1", "-2", "@SUM(A1)", "\t=x"])
async def test_the_csv_neutralises_formula_names(api, billing, name):
    world = await api.seed_org(plan_code="org_pro")
    user = uuid.uuid4()
    async with unit_of_work(api.sf) as s:
        await KnownUserRepository(s).upsert(user, "x@amity.edu", name, NOW)
    billing.rows = [row(user)]
    r = await get(api, world, fmt="csv")
    cells = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"), newline="")))[1]
    assert cells[1] == "'" + name and not cells[1].startswith(("=", "+", "-", "@", "\t"))


# ---------------------------------------------------------------- dates
async def test_date_rules(api, billing):
    world = await api.seed_org(plan_code="org_pro")
    assert (await get(api, world, start="2026-11-07", end="2026-11-01")).status_code == 422                 # to before from
    ninety_two = (date(2026, 11, 1) + timedelta(days=91)).isoformat()
    assert (await get(api, world, start="2026-11-01", end=ninety_two)).status_code == 200                    # 92 days inclusive
    ninety_three = (date(2026, 11, 1) + timedelta(days=92)).isoformat()
    assert (await get(api, world, start="2026-11-01", end=ninety_three)).status_code == 422
    assert (await get(api, world, start="2026-11-01", end="2026-11-01")).status_code == 200                  # one day
    oldest = (TODAY - timedelta(days=730)).isoformat()
    assert (await get(api, world, start=oldest, end=oldest)).status_code == 200
    too_old = (TODAY - timedelta(days=731)).isoformat()
    assert (await get(api, world, start=too_old, end=too_old)).status_code == 422                            # more than 2 years back
    for bad in ("2026-13-01", "yesterday", ""):
        assert (await get(api, world, start=bad)).status_code == 422
    assert (await api.client.get(f"/org/v1/orgs/{world.org.id}/usage", headers=api.h(world.admin))).status_code == 422
    assert (await get(api, world, fmt="xml")).status_code == 422
    assert len(billing.calls) == 3                                                                          # bad requests never reach billing


# ---------------------------------------------------------------- gating and permissions
@pytest.mark.parametrize("plan, ok", [("pilot", False), ("org_pro", True), ("org_max", True), ("payg", True)])
async def test_plan_gating(api, billing, plan, ok):
    world = await api.seed_org(plan_code=plan, status="pilot" if plan == "pilot" else "active")
    r = await get(api, world)
    assert (r.status_code == 200) is ok
    if not ok:
        assert r.status_code == 403 and r.json()["code"] == "FEATURE_NOT_IN_PLAN" and "usage_reports" in r.json()["detail"]
        assert billing.calls == []


async def test_who_may_read_usage(api, billing):
    world, other = await api.seed_org(plan_code="org_pro"), await api.seed_org()
    assert (await get(api, world, who=world.members[0])).status_code == 403
    assert (await get(api, world, who=other.owner)).status_code == 404
    assert (await get(api, world, who=world.owner)).status_code == 200
    assert (await api.client.get(url(world))).status_code == 401
    assert (await get(api, await api.seed_org(with_seat=False))).status_code == 403                        # no seat row: no paid features


# ---------------------------------------------------------------- billing failures are all 503
def total_seconds_count(route, result) -> float:
    return BILLING_REQUESTS.labels(route=route, result=result)._sum.get()


def assert_503(r):
    assert r.status_code == 503 and r.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert "billing" in r.json()["detail"].lower() and "test-token" not in r.text


async def test_billing_down_is_retried_once_then_503(api, billing):
    world = await api.seed_org(plan_code="org_pro")

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    billing.behaviour = refuse
    assert_503(await get(api, world))
    assert len(billing.calls) == 2                                                                           # one retry


async def test_a_5xx_is_retried_once_and_a_recovery_is_served(api, billing):
    world = await api.seed_org(plan_code="org_pro")
    billing.behaviour = lambda request: httpx.Response(502, text="bad gateway")
    assert_503(await get(api, world))
    assert len(billing.calls) == 2
    billing.calls.clear()
    answers = iter([httpx.Response(500), httpx.Response(200, json={"org_id": "x", "from": "a", "to": "b", "rows": []})])
    billing.behaviour = lambda request: next(answers)
    assert (await get(api, world)).status_code == 200 and len(billing.calls) == 2


async def test_a_timeout_is_503_and_not_retried(api, billing):
    world = await api.seed_org(plan_code="org_pro")

    def slow(request):
        raise httpx.ReadTimeout("took too long", request=request)

    billing.behaviour = slow
    assert_503(await get(api, world))
    assert len(billing.calls) == 1


async def test_a_401_from_billing_is_our_misconfiguration_logged_as_an_error_and_503(api, billing):
    world = await api.seed_org(plan_code="org_pro")
    billing.behaviour = lambda request: httpx.Response(401, json={"detail": "a service token is required"})
    with capture_logs() as logs:
        assert_503(await get(api, world))
    assert len(billing.calls) == 1
    assert any(entry["log_level"] == "error" and "service token" in entry["event"] for entry in logs)


@pytest.mark.parametrize("response", [httpx.Response(404, json={"detail": "Not Found"}), httpx.Response(422, json={"detail": "bad"}),
                                      httpx.Response(200, json={"unexpected": True}), httpx.Response(200, json=[1, 2])])
async def test_any_other_billing_answer_is_503(api, billing, response):
    world = await api.seed_org(plan_code="org_pro")
    billing.behaviour = lambda request: response
    assert_503(await get(api, world))


async def test_billing_calls_are_timed_by_route_and_result(api, billing):
    world = await api.seed_org(plan_code="org_pro")
    before_ok, before_5xx = total_seconds_count("usage", "2xx"), total_seconds_count("usage", "5xx")
    await get(api, world)
    billing.behaviour = lambda request: httpx.Response(500)
    await get(api, world)
    assert total_seconds_count("usage", "2xx") > before_ok and total_seconds_count("usage", "5xx") > before_5xx
