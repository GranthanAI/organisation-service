"""The staff routes at /admin/org/v1: create, members, seats, lifecycle, close, roles, reasons, idempotency."""

import uuid
from datetime import timedelta

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_clock, get_db_session, get_redis_client, get_session_factory_dep
from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.main import app
from app.models.audit_log import AuditLog
from app.models.invite import Invite
from app.models.job import Job
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.outbox import OutboxEvent
from app.models.policy import OrgPolicy
from app.models.seat_state import SeatState
from app.repositories.known_user_repository import KnownUserRepository
from app.services.idempotency_service import IdempotencyService
from app.workers.job_runner import JobRunner
from tests.support.factories import NOW
from tests.support.tokens import bearer, make_token

PREFIX = "/admin/org/v1/orgs"
GSTIN = "09AAAAA0000A1Z5"
REASON = "requested by the college"
OWNER_EMAIL = "dean@amity.edu"


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def staff():
    return bearer(make_token(roles=["admin", "billing_admin", "support"], email="staff@granthan.com"))


@pytest.fixture
def support():
    return bearer(make_token(roles=["support"], email="support@granthan.com"))


@pytest.fixture
async def env(session_factory, clock):
    async def session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_session_factory_dep] = lambda: session_factory
    app.dependency_overrides[get_redis_client] = lambda: fakeredis.FakeAsyncRedis()
    app.dependency_overrides[get_clock] = lambda: clock
    app.state.idempotency_service = IdempotencyService(session_factory, clock)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://org") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.idempotency_service = None


def body(**kw) -> dict:
    return {"name": "Amity University", "type": "college", "billing_email": "Accounts@Amity.edu",
            "owner_email": OWNER_EMAIL, "pilot": True, "reason": REASON, **kw}


async def post(client, path, json, headers, key=None):
    return await client.post(path, json=json, headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex})


async def create(client, staff, **kw):
    r = await post(client, PREFIX, body(**kw), staff)
    assert r.status_code == 201, r.text
    return r.json()


async def known_owner(sf, email=OWNER_EMAIL):
    user = uuid.uuid4()
    async with unit_of_work(sf) as s:
        await KnownUserRepository(s).upsert(user, email, "The Dean", NOW)
    return user


async def all_rows(sf, model):
    async with sf() as s:
        return list((await s.execute(select(model))).scalars())


# ---------------------------------------------------------------- create
async def test_create_a_pilot_with_a_known_owner_who_joins_at_once(env, session_factory, staff, clock):
    owner = await known_owner(session_factory)
    r = await post(env, PREFIX, body(legal_name="Amity Education Ltd", gstin=GSTIN, state_code="09"), staff)
    assert r.status_code == 201
    out = r.json()
    assert out["status"] == "pilot" and out["slug"] == "amity-university" and out["type"] == "college"
    assert out["pilot_ends_at"].startswith((NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S"))
    assert out["owner_joined"] is True and out["owner_invite_pending"] is False
    assert (out["gstin"], out["state_code"], out["billing_email"]) == (GSTIN, "09", "accounts@amity.edu")

    org_id = uuid.UUID(out["id"])
    (member,) = await all_rows(session_factory, Membership)
    assert (member.org_id, member.user_id, member.role, member.source, member.status) == (
        org_id, owner, "owner", "staff", "active")
    async with session_factory() as s:
        seat, policy = await s.get(SeatState, org_id), await s.get(OrgPolicy, org_id)
    assert (seat.plan_code, seat.seats_bought, seat.status, seat.source) == ("pilot", 25, "active", "staff")
    assert seat.period_end == NOW + timedelta(days=30)
    assert (policy.lock_modes, policy.version, policy.member_spend_cap_paise) == (False, 1, None)
    actions = sorted(a.action for a in await all_rows(session_factory, AuditLog))
    assert actions == ["member.joined", "org.created"]
    created = next(a for a in await all_rows(session_factory, AuditLog) if a.action == "org.created")
    assert created.actor_kind == "staff" and created.reason == REASON and created.org_id == org_id
    assert [e.topic for e in await all_rows(session_factory, OutboxEvent)] == ["org.member.joined"]


async def test_the_pilot_jobs_are_scheduled(env, session_factory, staff, clock):
    out = await create(env, staff)
    jobs = {j.dedupe_key: j for j in await all_rows(session_factory, Job) if j.kind.startswith("pilot.")}
    end = NOW + timedelta(days=30)
    org_id = out["id"]
    assert jobs[f"pilot.remind:{org_id}:7d"].run_at == end - timedelta(days=7)
    assert jobs[f"pilot.remind:{org_id}:1d"].run_at == end - timedelta(days=1)
    assert jobs[f"pilot.expire:{org_id}"].run_at == end
    assert jobs[f"pilot.remind:{org_id}:7d"].payload == {"org_id": org_id, "days_left": 7}


async def test_a_non_pilot_org_is_active_with_incomplete_seats_and_no_pilot_jobs(env, session_factory, staff):
    await known_owner(session_factory)
    out = await create(env, staff, pilot=False)
    assert out["status"] == "active" and out["pilot_ends_at"] is None and out["owner_joined"] is True
    async with session_factory() as s:
        seat = await s.get(SeatState, uuid.UUID(out["id"]))
    assert (seat.plan_code, seat.seats_bought, seat.status) == ("org_pro", None, "incomplete")
    assert [j for j in await all_rows(session_factory, Job) if j.kind.startswith("pilot.")] == []


async def test_an_unknown_owner_gets_an_open_invite_and_the_response_has_no_token(env, session_factory, staff, caplog):
    r = await post(env, PREFIX, body(), staff)
    out = r.json()
    assert out["owner_joined"] is False and out["owner_invite_pending"] is True
    assert await all_rows(session_factory, Membership) == []
    (invite,) = await all_rows(session_factory, Invite)
    assert (invite.kind, invite.role, invite.email, invite.accepted_at, invite.revoked_at) == (
        "email", "owner", OWNER_EMAIL, None, None)
    assert invite.expires_at == NOW + timedelta(days=14) and len(invite.token_hash) == 64
    assert "token" not in r.text.lower()
    assert invite.token_hash not in r.text and invite.token_hash not in caplog.text
    assert "invite.created" in [a.action for a in await all_rows(session_factory, AuditLog)]
    assert [e for e in await all_rows(session_factory, OutboxEvent)] == []         # nothing is sent before Phase 7


async def test_a_deleted_known_user_is_not_the_owner_either(env, session_factory, staff):
    user = await known_owner(session_factory)
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).set_deleted(user, NOW)
    out = await create(env, staff)
    assert out["owner_invite_pending"] is True and out["owner_joined"] is False


async def test_a_slug_clash_gets_a_numbered_suffix(env, session_factory, staff):
    slugs = [(await create(env, staff, owner_email=f"o{i}@x.edu"))["slug"] for i in range(3)]
    assert slugs == ["amity-university", "amity-university-2", "amity-university-3"]


@pytest.mark.parametrize("name, slug", [("St. Xavier's College, Mumbai!", "st-xavier-s-college-mumbai"),
                                        ("  IIT   Delhi  ", "iit-delhi"), ("A!", "org-a"), ("Ünï Ørg", "n-rg")])
async def test_the_slug_is_a_clean_a_z_0_9_dash_string(env, staff, name, slug):
    out = await create(env, staff, name=name)
    assert out["slug"] == slug and 3 <= len(slug) <= 60


async def test_a_very_long_name_is_cut_to_60_characters(env, staff):
    out = await create(env, staff, name="Very Long Institute " * 10)
    assert len(out["slug"]) <= 60 and not out["slug"].endswith("-")


@pytest.mark.parametrize("extra", [
    {"gstin": "NOTAGSTIN"}, {"gstin": "09AAAAA0000A1Z"}, {"gstin": "99AAAAA0000A1Z5x"}, {"gstin": "00AAAAA0000A1Z5"},
    {"state_code": "00"}, {"state_code": "25"}, {"state_code": "9"}, {"state_code": "ZZ"}, {"type": "school"},
    {"billing_email": "nope"}, {"owner_email": "nope"}, {"name": "   "},
])
async def test_validation_errors_create_nothing(env, session_factory, staff, extra):
    r = await post(env, PREFIX, body(**extra), staff)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    assert await all_rows(session_factory, Organisation) == []


@pytest.mark.parametrize("state_code", ["01", "09", "27", "29", "33", "36", "97", "99"])
async def test_gst_state_codes_are_accepted(env, staff, state_code):
    assert (await create(env, staff, state_code=state_code))["state_code"] == state_code


async def test_a_lower_case_gstin_is_normalised(env, staff):
    assert (await create(env, staff, gstin=GSTIN.lower()))["gstin"] == GSTIN


# ---------------------------------------------------------------- members and the pilot cap
async def test_the_26th_pilot_member_is_refused_with_pilot_full(env, session_factory, staff):
    await known_owner(session_factory)
    org = (await create(env, staff))["id"]                                   # the owner is member 1
    for _ in range(24):
        assert (await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
                ).status_code == 201
    r = await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
    assert r.status_code == 409 and r.json()["code"] == "PILOT_FULL"
    assert len(await all_rows(session_factory, Membership)) == 25


async def test_add_a_member_returns_it_and_audits_as_staff(env, session_factory, staff):
    org = (await create(env, staff))["id"]
    user = uuid.uuid4()
    r = await post(env, f"{PREFIX}/{org}/members", {"user_id": str(user), "role": "admin", "reason": REASON}, staff)
    assert r.status_code == 201
    assert {k: r.json()[k] for k in ("org_id", "user_id", "role", "status", "source")} == {
        "org_id": org, "user_id": str(user), "role": "admin", "status": "active", "source": "staff"}
    joined = [a for a in await all_rows(session_factory, AuditLog) if a.action == "member.joined"]
    assert joined[-1].actor_kind == "staff" and joined[-1].details["role"] == "admin"


@pytest.mark.parametrize("payload, status, code", [
    ({"role": "boss"}, 422, "VALIDATION_ERROR"),
])
async def test_add_member_validates_the_role(env, staff, payload, status, code):
    org = (await create(env, staff))["id"]
    r = await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON, **payload}, staff)
    assert (r.status_code, r.json()["code"]) == (status, code)


async def test_a_user_already_in_an_org_cannot_be_added_to_another(env, session_factory, staff):
    a = (await create(env, staff, owner_email="a@x.edu"))["id"]
    b = (await create(env, staff, owner_email="b@x.edu"))["id"]
    user = str(uuid.uuid4())
    assert (await post(env, f"{PREFIX}/{a}/members", {"user_id": user, "reason": REASON}, staff)).status_code == 201
    r = await post(env, f"{PREFIX}/{b}/members", {"user_id": user, "reason": REASON}, staff)
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_IN_ORG"


async def test_adding_to_an_unknown_org_is_404(env, staff):
    r = await post(env, f"{PREFIX}/{uuid.uuid4()}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- seats
async def test_seats_are_needed_before_members_join_a_non_pilot_org(env, session_factory, staff):
    org = (await create(env, staff, pilot=False))["id"]
    r = await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
    assert r.status_code == 409 and r.json()["code"] == "SEATS_FULL"          # 'incomplete' = no seats yet
    low = await post(env, f"{PREFIX}/{org}/seats", {"plan_code": "org_pro", "variant": "college", "seats_bought": 49,
                                                   "reason": REASON}, staff)
    assert low.status_code == 422 and "50" in low.json()["errors"][0]["message"] if "errors" in low.json() else True
    ok = await post(env, f"{PREFIX}/{org}/seats", {"plan_code": "org_pro", "variant": "college", "seats_bought": 50,
                                                  "period_end": "2027-11-21T00:00:00Z", "reason": REASON}, staff)
    assert ok.status_code == 200
    assert (ok.json()["plan_code"], ok.json()["seats_bought"], ok.json()["status"], ok.json()["source"]) == (
        "org_pro", 50, "active", "staff")
    again = await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
    assert again.status_code == 201


async def test_the_minimum_seats_error_names_the_minimum(env, staff):
    org = (await create(env, staff, pilot=False))["id"]
    r = await post(env, f"{PREFIX}/{org}/seats", {"plan_code": "org_pro", "variant": "college", "seats_bought": 49,
                                                 "reason": REASON}, staff)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR" and "at least 50" in r.json()["detail"]


async def test_a_paid_plan_converts_a_pilot_to_active(env, session_factory, staff):
    org = (await create(env, staff))["id"]
    r = await post(env, f"{PREFIX}/{org}/seats", {"plan_code": "payg", "variant": "college", "reason": REASON}, staff)
    assert r.status_code == 200 and r.json()["seats_bought"] is None
    detail = (await env.get(f"{PREFIX}/{org}", headers=staff)).json()
    assert detail["status"] == "active" and detail["seat_state"]["plan_code"] == "payg"


# ---------------------------------------------------------------- suspend, reactivate, extend
async def test_suspend_then_reactivate_a_pilot_goes_back_to_pilot(env, staff):
    org = (await create(env, staff))["id"]
    s = await post(env, f"{PREFIX}/{org}/suspend", {"reason": REASON}, staff)
    assert s.status_code == 200 and s.json()["status"] == "suspended"
    r = await post(env, f"{PREFIX}/{org}/reactivate", {"reason": REASON}, staff)
    assert r.json()["status"] == "pilot"


async def test_reactivating_after_the_pilot_ended_goes_to_pilot_expired(env, staff, clock):
    org = (await create(env, staff))["id"]
    await post(env, f"{PREFIX}/{org}/suspend", {"reason": REASON}, staff)
    clock.advance(days=31)
    r = await post(env, f"{PREFIX}/{org}/reactivate", {"reason": REASON}, staff)
    assert r.status_code == 200 and r.json()["status"] == "pilot_expired"


async def test_an_active_org_suspends_and_reactivates_to_active(env, staff):
    org = (await create(env, staff, pilot=False))["id"]
    assert (await post(env, f"{PREFIX}/{org}/suspend", {"reason": REASON}, staff)).json()["status"] == "suspended"
    assert (await post(env, f"{PREFIX}/{org}/reactivate", {"reason": REASON}, staff)).json()["status"] == "active"


async def test_the_audit_remembers_whether_it_was_a_pilot(env, session_factory, staff):
    org = (await create(env, staff))["id"]
    await post(env, f"{PREFIX}/{org}/suspend", {"reason": REASON}, staff)
    row = [a for a in await all_rows(session_factory, AuditLog) if a.action == "org.status_changed"][-1]
    assert row.details["from"] == "pilot" and row.details["to"] == "suspended" and row.details["was_pilot"] is True


@pytest.mark.parametrize("first, second", [("reactivate", None), ("suspend", "suspend"), ("close", "suspend"),
                                           ("close", "close"), ("close", "reactivate")])
async def test_illegal_lifecycle_moves_are_422(env, staff, first, second):
    org = (await create(env, staff, pilot=False))["id"]
    if second is None:
        r = await post(env, f"{PREFIX}/{org}/{first}", {"reason": REASON}, staff)
    else:
        await post(env, f"{PREFIX}/{org}/{first}", {"reason": REASON}, staff)
        r = await post(env, f"{PREFIX}/{org}/{second}", {"reason": REASON}, staff)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


async def test_extend_an_expired_pilot_returns_it_to_pilot_and_reschedules_the_jobs(env, session_factory, staff, clock):
    org = (await create(env, staff))["id"]
    clock.advance(days=31)
    await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once()          # pilot.expire runs
    assert (await env.get(f"{PREFIX}/{org}", headers=staff)).json()["status"] == "pilot_expired"

    r = await post(env, f"{PREFIX}/{org}/extend-pilot", {"days": 10, "reason": REASON}, staff)
    assert r.status_code == 200 and r.json()["status"] == "pilot"
    assert r.json()["pilot_ends_at"].startswith((clock.now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"))
    jobs = {j.dedupe_key: j for j in await all_rows(session_factory, Job) if j.kind.startswith("pilot.") and j.done_at is None}
    assert jobs[f"pilot.expire:{org}"].run_at == clock.now() + timedelta(days=10)
    assert jobs[f"pilot.remind:{org}:7d"].run_at == clock.now() + timedelta(days=3)
    assert f"pilot.remind:{org}:1d" in jobs


async def test_extending_a_running_pilot_adds_to_its_end(env, staff, clock):
    out = await create(env, staff)
    clock.advance(days=5)
    r = await post(env, f"{PREFIX}/{out['id']}/extend-pilot", {"days": 7, "reason": REASON}, staff)
    assert r.json()["pilot_ends_at"].startswith((NOW + timedelta(days=37)).strftime("%Y-%m-%dT%H:%M"))
    seat = (await env.get(f"{PREFIX}/{out['id']}", headers=staff)).json()["seat_state"]
    assert seat["period_end"].startswith((NOW + timedelta(days=37)).strftime("%Y-%m-%dT%H:%M"))


@pytest.mark.parametrize("days", [0, 31, -1])
async def test_extend_days_must_be_1_to_30(env, staff, days):
    org = (await create(env, staff))["id"]
    r = await post(env, f"{PREFIX}/{org}/extend-pilot", {"days": days, "reason": REASON}, staff)
    assert r.status_code == 422


async def test_only_pilots_can_be_extended(env, staff):
    org = (await create(env, staff, pilot=False))["id"]
    r = await post(env, f"{PREFIX}/{org}/extend-pilot", {"days": 5, "reason": REASON}, staff)
    assert r.status_code == 422 and "pilot" in r.json()["detail"]


# ---------------------------------------------------------------- close
async def test_close_blocks_joins_at_once_revokes_invites_and_a_job_ends_the_members(
        env, session_factory, staff, clock, monkeypatch):
    from app.core import constants
    monkeypatch.setattr(constants, "CLOSE_BATCH_SIZE", 2)                # five members end in three transactions
    await known_owner(session_factory)
    out = await create(env, staff, pilot=False)
    org = out["id"]
    await post(env, f"{PREFIX}/{org}/seats", {"plan_code": "org_pro", "variant": "college", "seats_bought": 60,
                                             "reason": REASON}, staff)
    users = [uuid.uuid4() for _ in range(4)]
    for u in users:
        await post(env, f"{PREFIX}/{org}/members", {"user_id": str(u), "reason": REASON}, staff)
    async with unit_of_work(session_factory) as s:
        from tests.support.factories import make_invite
        s.add(make_invite(uuid.UUID(org), email="late@amity.edu", token_hash="late-1"))
        s.add(make_invite(uuid.UUID(org), kind="link", email=None, token_hash="late-2"))

    r = await post(env, f"{PREFIX}/{org}/close", {"reason": REASON}, staff)
    assert r.status_code == 200 and r.json()["status"] == "closed" and r.json()["closed_at"] is not None

    # at once: joins are blocked, invites are revoked, the members are still members until the job runs
    again = await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
    assert again.status_code == 409 and again.json()["code"] == "ORG_INACTIVE"
    invites = [i for i in await all_rows(session_factory, Invite) if i.org_id == uuid.UUID(org)]
    assert all(i.revoked_at == NOW and i.revoke_reason == "revoked" for i in invites if i.token_hash.startswith("late"))
    assert len([m for m in await all_rows(session_factory, Membership) if m.status == "active"]) == 5
    (job,) = [j for j in await all_rows(session_factory, Job) if j.kind == "org.close_members"]
    assert job.payload == {"org_id": org} and job.done_at is None

    # the job ends them, in batches, and emits the left events
    runner = JobRunner(session_factory, None, clock, poll_seconds=0.05)
    assert await runner.run_once() == 1
    members = await all_rows(session_factory, Membership)
    assert {m.status for m in members} == {"removed"} and {m.end_reason for m in members} == {"org_closed"}
    left = [e for e in await all_rows(session_factory, OutboxEvent) if e.topic == "org.member.left"]
    assert len(left) == 5 and {e.payload["reason"] for e in left} == {"org_closed"}
    assert {e.payload["org_type"] for e in left} == {"college"}
    (job,) = [j for j in await all_rows(session_factory, Job) if j.kind == "org.close_members"]
    assert job.done_at is not None


async def test_the_close_job_resumes_where_it_stopped(env, session_factory, staff, clock, monkeypatch):
    from app.core import constants
    from app.services import org_jobs
    monkeypatch.setattr(constants, "CLOSE_BATCH_SIZE", 2)
    org = (await create(env, staff))["id"]
    for _ in range(5):
        await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
    await post(env, f"{PREFIX}/{org}/close", {"reason": REASON}, staff)

    real_end = org_jobs.membership_service.end
    calls = {"n": 0}

    async def crash_on_the_fourth(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("process died")
        return await real_end(*args, **kwargs)

    monkeypatch.setattr(org_jobs.membership_service, "end", crash_on_the_fourth)
    runner = JobRunner(session_factory, None, clock, poll_seconds=0.05)
    await runner.run_once()                                                       # batch 1 (2 members) committed; batch 2 failed
    active = [m for m in await all_rows(session_factory, Membership) if m.status == "active"]
    assert len(active) == 3 and any(m.status == "removed" for m in await all_rows(session_factory, Membership))
    monkeypatch.setattr(org_jobs.membership_service, "end", real_end)
    clock.advance(seconds=5)
    await runner.run_once()                                                       # the retry finishes the rest, once
    assert {m.status for m in await all_rows(session_factory, Membership)} == {"removed"}
    left = [e for e in await all_rows(session_factory, OutboxEvent) if e.topic == "org.member.left"]
    assert len(left) == 5 == len({e.key for e in left})                           # the 5 members, each exactly once


# ---------------------------------------------------------------- roles, reasons, idempotency
WRITES = [("POST", "", "create"), ("POST", "/{id}/members", "member"), ("POST", "/{id}/seats", "seats"),
          ("POST", "/{id}/suspend", "reason"), ("POST", "/{id}/reactivate", "reason"),
          ("POST", "/{id}/extend-pilot", "extend"), ("POST", "/{id}/close", "reason")]


def write_body(kind: str) -> dict:
    return {"create": body(), "member": {"user_id": str(uuid.uuid4()), "reason": REASON},
            "seats": {"plan_code": "payg", "variant": "college", "reason": REASON},
            "reason": {"reason": REASON}, "extend": {"days": 5, "reason": REASON}}[kind]


@pytest.mark.parametrize("method, path, kind", WRITES)
async def test_support_cannot_write_and_a_plain_user_cannot_either(env, staff, support, method, path, kind):
    org = (await create(env, staff))["id"]
    url = PREFIX + path.format(id=org)
    for headers in (support, bearer(make_token(roles=[])), bearer(make_token(roles=["billing_admin"]))):
        r = await post(env, url, write_body(kind), headers)
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    r = await env.post(url, json=write_body(kind), headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 401


async def test_support_can_read_but_a_plain_user_cannot(env, session_factory, staff, support):
    org = (await create(env, staff))["id"]
    assert (await env.get(PREFIX, headers=support)).status_code == 200
    assert (await env.get(f"{PREFIX}/{org}", headers=support)).status_code == 200
    plain = bearer(make_token(roles=[]))
    assert (await env.get(PREFIX, headers=plain)).status_code == 403
    assert (await env.get(f"{PREFIX}/{org}", headers=plain)).status_code == 403
    assert (await env.get(PREFIX)).status_code == 401


@pytest.mark.parametrize("method, path, kind", [w for w in WRITES if w[2] != "create"] + [("POST", "", "create")])
async def test_a_missing_or_short_reason_is_422(env, staff, method, path, kind):
    org = (await create(env, staff))["id"]
    url = PREFIX + path.format(id=org)
    good = write_body(kind)
    for bad in ({k: v for k, v in good.items() if k != "reason"}, {**good, "reason": "no"}, {**good, "reason": "x" * 501},
                {**good, "reason": "     "}):
        r = await post(env, url, bad, staff)
        assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR", (kind, bad.get("reason"))
        assert "reason" in {e["field"] for e in r.json()["errors"]}


async def test_unknown_fields_are_refused(env, staff):
    r = await post(env, PREFIX, body(is_admin=True), staff)
    assert r.status_code == 422


async def test_an_idempotency_key_is_required_on_every_write(env, staff):
    r = await env.post(PREFIX, json=body(), headers=staff)
    assert r.status_code == 422 and "Idempotency-Key" in r.json()["detail"]


async def test_the_same_key_creates_one_org_and_replays_the_answer(env, session_factory, staff):
    key = "create-amity-0001"
    first = await post(env, PREFIX, body(), staff, key=key)
    second = await post(env, PREFIX, body(), staff, key=key)
    assert first.status_code == second.status_code == 201 and first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert len(await all_rows(session_factory, Organisation)) == 1
    different = await post(env, PREFIX, body(name="Another"), staff, key=key)
    assert different.status_code == 422 and different.json()["code"] == "IDEMPOTENCY_MISMATCH"
    assert len(await all_rows(session_factory, Organisation)) == 1


async def test_a_failed_write_can_be_retried_with_the_same_key(env, staff):
    org = (await create(env, staff))["id"]
    key = "suspend-retry-0001"
    r1 = await post(env, f"{PREFIX}/{org}/reactivate", {"reason": REASON}, staff, key=key)     # not suspended: 422
    assert r1.status_code == 422
    await post(env, f"{PREFIX}/{org}/suspend", {"reason": REASON}, staff)
    r2 = await post(env, f"{PREFIX}/{org}/reactivate", {"reason": REASON}, staff, key=key)     # the retry now works
    assert r2.status_code == 200 and "idempotent-replayed" not in r2.headers


# ---------------------------------------------------------------- reads
async def test_get_one_includes_the_seat_state_and_the_active_count(env, session_factory, staff):
    await known_owner(session_factory)
    org = (await create(env, staff))["id"]
    await post(env, f"{PREFIX}/{org}/members", {"user_id": str(uuid.uuid4()), "reason": REASON}, staff)
    body_ = (await env.get(f"{PREFIX}/{org}", headers=staff)).json()
    assert body_["active_count"] == 2 and body_["seat_state"]["plan_code"] == "pilot"
    assert body_["seat_state"]["seats_bought"] == 25 and body_["name"] == "Amity University"
    assert (await env.get(f"{PREFIX}/{uuid.uuid4()}", headers=staff)).status_code == 404


async def test_search_by_name_slug_and_status_with_a_stable_cursor(env, session_factory, staff, clock):
    names = ["Amity University", "Acme Corp", "Beta College", "Gamma Institute", "Delta Labs"]
    for i, name in enumerate(names):
        clock.advance(seconds=1)
        await create(env, staff, name=name, owner_email=f"o{i}@x.edu", pilot=(i % 2 == 0), type="company" if i == 1 else "college")
    page1 = (await env.get(f"{PREFIX}?limit=2", headers=staff)).json()
    assert [o["name"] for o in page1["items"]] == ["Delta Labs", "Gamma Institute"]            # newest first
    page2 = (await env.get(f"{PREFIX}?limit=2&cursor={page1['next_cursor']}", headers=staff)).json()
    page3 = (await env.get(f"{PREFIX}?limit=2&cursor={page2['next_cursor']}", headers=staff)).json()
    seen = [o["name"] for p in (page1, page2, page3) for o in p["items"]]
    assert seen == ["Delta Labs", "Gamma Institute", "Beta College", "Acme Corp", "Amity University"]
    assert page3["next_cursor"] is None
    assert [o["name"] for o in (await env.get(f"{PREFIX}?q=amity", headers=staff)).json()["items"]] == ["Amity University"]
    assert [o["slug"] for o in (await env.get(f"{PREFIX}?q=acme-co", headers=staff)).json()["items"]] == ["acme-corp"]
    pilots = (await env.get(f"{PREFIX}?status=pilot", headers=staff)).json()["items"]
    assert {o["status"] for o in pilots} == {"pilot"} and len(pilots) == 3
    assert (await env.get(f"{PREFIX}?status=nonsense", headers=staff)).status_code == 422
    assert (await env.get(f"{PREFIX}?cursor=garbage", headers=staff)).status_code == 422
