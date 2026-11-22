"""GET and PUT /org/v1/orgs/{id}/policies: ETag and If-Match, plan gating, validation, the event and the audit row."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.outbox import OutboxEvent
from app.models.policy import OrgPolicy
from tests.support.factories import NOW


def url(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/policies"


def put_headers(api, who, if_match='"v1"'):
    headers = {**api.h(who), "Idempotency-Key": uuid.uuid4().hex}
    if if_match is not None:
        headers["If-Match"] = if_match
    return headers


async def put(api, world, body, who=None, if_match='"v1"'):
    return await api.client.put(url(world), json=body, headers=put_headers(api, who or world.admin, if_match))


async def table(api, model):
    async with api.sf() as s:
        return list((await s.execute(select(model))).scalars())


def modes(n=2):
    return [str(uuid.uuid4()) for _ in range(n)]


# ---------------------------------------------------------------- GET
async def test_get_returns_the_default_with_an_etag_and_members_may_read(api):
    world = await api.seed_org()
    for who in (world.members[0], world.admin, world.owner):
        r = await api.client.get(url(world), headers=api.h(who))
        assert r.status_code == 200 and r.headers["etag"] == '"v1"'
        assert r.json() == {"version": 1, "lock_modes": False, "approved_mode_ids": [], "member_spend_cap_paise": None,
                            "updated_at": None, "updated_by": None}


async def test_get_of_another_orgs_policy_is_404(api):
    world, other = await api.seed_org(), await api.seed_org()
    assert (await api.client.get(url(world), headers=api.h(other.owner))).status_code == 404
    assert (await api.client.get(url(world))).status_code == 401


# ---------------------------------------------------------------- PUT
async def test_put_bumps_the_version_audits_and_enqueues_the_event(api):
    world = await api.seed_org(plan_code="org_pro")
    ids = modes(3)
    r = await put(api, world, {"lock_modes": True, "approved_mode_ids": ids, "member_spend_cap_paise": None})
    assert r.status_code == 200 and r.headers["etag"] == '"v2"'
    assert r.json() == {"version": 2, "lock_modes": True, "approved_mode_ids": ids, "member_spend_cap_paise": None,
                        "updated_at": r.json()["updated_at"], "updated_by": str(world.admin.id)}
    assert r.json()["updated_at"].startswith(NOW.strftime("%Y-%m-%dT%H:%M"))
    got = await api.client.get(url(world), headers=api.h(world.members[0]))
    assert got.headers["etag"] == '"v2"' and got.json()["approved_mode_ids"] == ids

    (event,) = [e for e in await table(api, OutboxEvent) if e.topic == "org.policy.changed"]
    assert event.key == str(world.org.id)                                                    # keyed by org_id
    payload = event.payload
    assert (payload["org_id"], payload["version"], payload["lock_modes"], payload["approved_mode_ids"],
            payload["member_spend_cap_paise"]) == (str(world.org.id), 2, True, ids, None)    # the FULL policy
    assert payload["schema_version"] == "1.0"
    (audit,) = [a for a in await table(api, AuditLog) if a.action == "policy.changed"]
    assert audit.actor_id == world.admin.id and audit.org_id == world.org.id
    assert audit.details["old"] == {"lock_modes": False, "approved_mode_ids": [], "member_spend_cap_paise": None, "version": 1}
    assert audit.details["new"] == {"lock_modes": True, "approved_mode_ids": ids, "member_spend_cap_paise": None, "version": 2}


async def test_the_second_put_needs_the_new_version(api):
    world = await api.seed_org(plan_code="org_pro")
    assert (await put(api, world, {"lock_modes": False})).status_code == 200
    stale = await put(api, world, {"lock_modes": False}, if_match='"v1"')
    assert stale.status_code == 412 and stale.json()["code"] == "VERSION_CONFLICT" and stale.json()["current_version"] == 2
    ok = await put(api, world, {"lock_modes": True, "approved_mode_ids": modes(1)}, if_match='"v2"')
    assert ok.status_code == 200 and ok.json()["version"] == 3
    assert len([e for e in await table(api, OutboxEvent) if e.topic == "org.policy.changed"]) == 2      # the 412 queued nothing


@pytest.mark.parametrize("header", ['"v1"', "v1", "1", 'W/"v1"', '"V1"'])
async def test_if_match_formats(api, header):
    world = await api.seed_org(plan_code="org_pro")
    assert (await put(api, world, {"lock_modes": False}, if_match=header)).status_code == 200


async def test_a_missing_if_match_is_428_and_a_malformed_one_is_422(api):
    world = await api.seed_org(plan_code="org_pro")
    r = await put(api, world, {"lock_modes": False}, if_match=None)
    assert r.status_code == 428 and r.json()["code"] == "PRECONDITION_REQUIRED"
    assert (await put(api, world, {"lock_modes": False}, if_match="banana")).status_code == 422
    assert await table(api, OrgPolicy) == [] and [e for e in await table(api, OutboxEvent) if e.topic == "org.policy.changed"] == []


async def test_two_puts_at_the_same_version_give_one_winner(api):
    world = await api.seed_org(plan_code="org_pro")
    results = await asyncio.gather(put(api, world, {"lock_modes": False, "member_spend_cap_paise": None}),
                                   put(api, world, {"lock_modes": True, "approved_mode_ids": modes(1)}, who=world.owner))
    assert sorted(r.status_code for r in results) == [200, 412]
    assert len([e for e in await table(api, OutboxEvent) if e.topic == "org.policy.changed"]) == 1


async def test_an_existing_policy_row_is_updated_in_place(api):
    world = await api.seed_org(plan_code="org_pro")
    async with unit_of_work(api.sf) as s:                                                       # what staff org creation writes
        s.add(OrgPolicy(org_id=world.org.id, lock_modes=False, approved_mode_ids=[], member_spend_cap_paise=None, version=1,
                        updated_at=NOW))
    assert (await put(api, world, {"lock_modes": False})).status_code == 200
    (row,) = await table(api, OrgPolicy)
    assert row.version == 2 and row.updated_by == world.admin.id


# ---------------------------------------------------------------- plan gating
@pytest.mark.parametrize("plan, body, status, feature", [
    ("pilot", {"lock_modes": True, "approved_mode_ids": "m"}, 403, "lock_modes"),
    ("org_pro", {"lock_modes": True, "approved_mode_ids": "m"}, 200, None),
    ("org_max", {"lock_modes": True, "approved_mode_ids": "m"}, 200, None),
    ("payg", {"lock_modes": True, "approved_mode_ids": "m"}, 200, None),
    ("org_pro", {"lock_modes": False, "member_spend_cap_paise": 50000}, 403, "spend_cap"),
    ("org_max", {"lock_modes": False, "member_spend_cap_paise": 50000}, 403, "spend_cap"),
    ("pilot", {"lock_modes": False, "member_spend_cap_paise": 50000}, 403, "spend_cap"),
    ("payg", {"lock_modes": False, "member_spend_cap_paise": 50000}, 200, None),
    ("pilot", {"lock_modes": False}, 200, None),
])
async def test_plan_gating(api, plan, body, status, feature):
    world = await api.seed_org(plan_code=plan, status="pilot" if plan == "pilot" else "active")
    if body.get("approved_mode_ids") == "m":
        body = {**body, "approved_mode_ids": modes(1)}
    r = await put(api, world, body)
    assert r.status_code == status
    if feature:
        assert r.json()["code"] == "FEATURE_NOT_IN_PLAN" and feature in r.json()["detail"]
        assert await table(api, OrgPolicy) == []


async def test_an_org_with_no_seat_row_has_no_paid_features(api):
    world = await api.seed_org(with_seat=False)
    r = await put(api, world, {"lock_modes": True, "approved_mode_ids": modes(1)})
    assert r.status_code == 403 and r.json()["code"] == "FEATURE_NOT_IN_PLAN"
    assert (await put(api, world, {"lock_modes": False})).status_code == 200


# ---------------------------------------------------------------- validation
async def test_lock_modes_needs_an_approved_mode(api):
    world = await api.seed_org(plan_code="org_pro")
    r = await put(api, world, {"lock_modes": True, "approved_mode_ids": []})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR" and "approved mode" in r.json()["detail"]
    assert (await put(api, world, {"lock_modes": True})).status_code == 422


async def test_approved_ids_are_deduplicated_and_capped_at_50(api):
    world = await api.seed_org(plan_code="org_pro")
    a, b = modes(2)
    r = await put(api, world, {"lock_modes": True, "approved_mode_ids": [a, b, a, b, a]})
    assert r.status_code == 200 and r.json()["approved_mode_ids"] == [a, b]                   # first occurrence wins
    ok = await put(api, world, {"lock_modes": True, "approved_mode_ids": modes(50)}, if_match='"v2"')
    assert ok.status_code == 200 and len(ok.json()["approved_mode_ids"]) == 50
    too_many = await put(api, world, {"lock_modes": True, "approved_mode_ids": modes(51)}, if_match='"v3"')
    assert too_many.status_code == 422
    same_50 = modes(1) * 60                                                                    # 60 entries, 1 distinct: fine
    assert (await put(api, world, {"lock_modes": True, "approved_mode_ids": same_50}, if_match='"v3"')).status_code == 200


@pytest.mark.parametrize("cap, ok", [(99, False), (100, True), (10_000_000, True), (10_000_001, False), (0, False), (-5, False),
                                     (None, True)])
async def test_the_spend_cap_range(api, cap, ok):
    world = await api.seed_org(plan_code="payg")
    r = await put(api, world, {"lock_modes": False, "member_spend_cap_paise": cap})
    assert (r.status_code == 200) is ok
    if ok:
        assert r.json()["member_spend_cap_paise"] == cap
    else:
        assert r.status_code == 422


async def test_unknown_fields_and_bad_ids_are_422(api):
    world = await api.seed_org(plan_code="org_pro")
    assert (await put(api, world, {"lock_modes": False, "bogus": 1})).status_code == 422
    assert (await put(api, world, {"lock_modes": False, "approved_mode_ids": ["not-a-uuid"]})).status_code == 422
    assert (await put(api, world, {})).status_code == 422                                      # a full replace names lock_modes


# ---------------------------------------------------------------- who and when
async def test_a_member_cannot_put_and_another_org_gets_404(api):
    world, other = await api.seed_org(plan_code="org_pro"), await api.seed_org()
    assert (await put(api, world, {"lock_modes": False}, who=world.members[0])).status_code == 403
    assert (await put(api, world, {"lock_modes": False}, who=other.owner)).status_code == 404


async def test_a_closed_org_refuses_and_a_suspended_one_may_still_tidy_up(api):
    closed = await api.seed_org(plan_code="org_pro", status="closed")
    r = await put(api, closed, {"lock_modes": False})
    assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"
    suspended = await api.seed_org(plan_code="org_pro", status="suspended")
    assert (await put(api, suspended, {"lock_modes": False})).status_code == 200


async def test_billing_reads_the_spend_cap_from_the_internal_org_route(api):
    world = await api.seed_org(plan_code="payg")
    assert (await put(api, world, {"lock_modes": False, "member_spend_cap_paise": 250000})).status_code == 200
    r = await api.client.get(f"/internal/orgs/{world.org.id}", headers={"Authorization": "Bearer dev-billing-token"})
    assert r.status_code == 200 and r.json()["member_spend_cap_paise"] == 250000
