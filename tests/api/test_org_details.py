"""GET and PATCH /org/v1/orgs/{org_id}."""

import uuid

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.organisation import Organisation

GSTIN = "09AAAAA0000A1Z5"


def url(world) -> str:
    return f"/org/v1/orgs/{world.org.id}"


async def audits(api, action="org.details_changed"):
    async with api.sf() as s:
        return [a for a in (await s.execute(select(AuditLog).order_by(AuditLog.at))).scalars() if a.action == action]


async def row(api, world) -> Organisation:
    async with api.sf() as s:
        return await s.get(Organisation, world.org.id)


# ---------------------------------------------------------------- read
async def test_every_member_can_read_the_details_with_no_audit_and_no_seat_details(api):
    world = await api.seed_org(name="Amity University", legal_name="Amity Education Ltd", gstin=GSTIN, state_code="09",
                               billing_email="accounts@amity.edu")
    for person in (world.owner, world.admin, world.member):
        r = await api.client.get(url(world), headers=api.h(person))
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"id", "name", "slug", "type", "status", "legal_name", "gstin", "state_code",
                             "billing_email", "pilot_ends_at", "created_at"}
        assert (body["name"], body["legal_name"], body["gstin"], body["state_code"], body["billing_email"]) == (
            "Amity University", "Amity Education Ltd", GSTIN, "09", "accounts@amity.edu")
        assert body["id"] == str(world.org.id) and body["slug"] == world.org.slug
    assert await audits(api, "org.details_changed") == [] and not [a for a in await audits(api, "org.viewed")]


async def test_a_non_member_gets_404_not_403_on_every_route(api):
    world = await api.seed_org()
    stranger = api.h(uuid.uuid4())
    other = await api.seed_org()
    for headers in (stranger, api.h(other.owner)):
        assert (await api.client.get(url(world), headers=headers)).status_code == 404
        assert (await api.client.patch(url(world), json={"name": "Hacked"}, headers=headers)).status_code == 404
    assert (await row(api, world)).name == world.org.name


async def test_no_token_is_401(api):
    world = await api.seed_org()
    assert (await api.client.get(url(world))).status_code == 401
    assert (await api.client.patch(url(world), json={"name": "Abc"})).status_code == 401


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_reading_works_in_every_status(api, status):
    world = await api.seed_org(status=status)
    r = await api.client.get(url(world), headers=api.h(world.member))
    assert r.status_code == 200 and r.json()["status"] == status


# ---------------------------------------------------------------- patch
async def test_a_member_cannot_patch(api):
    world = await api.seed_org()
    r = await api.client.patch(url(world), json={"name": "New Name"}, headers=api.h(world.member))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    assert (await row(api, world)).name == world.org.name


@pytest.mark.parametrize("who", ["owner", "admin"])
async def test_owner_and_admin_can_patch(api, who):
    world = await api.seed_org(name="Old Name")
    r = await api.client.patch(url(world), headers=api.h(getattr(world, who)),
                               json={"name": "  New Name  ", "legal_name": "New Name Ltd", "gstin": GSTIN,
                                     "state_code": "09", "billing_email": "Finance@Amity.EDU"})
    assert r.status_code == 200
    out = r.json()
    assert (out["name"], out["legal_name"], out["gstin"], out["state_code"], out["billing_email"]) == (
        "New Name", "New Name Ltd", GSTIN, "09", "finance@amity.edu")
    assert out["slug"] == world.org.slug                                     # the slug never changes
    saved = await row(api, world)
    assert saved.name == "New Name" and saved.updated_at == api.clock.now()


async def test_the_audit_lists_only_the_changed_fields_old_and_new(api):
    world = await api.seed_org(name="Old Name", legal_name="Same Ltd", state_code="27", gstin=None)
    await api.client.patch(url(world), headers=api.h(world.admin),
                           json={"name": "New Name", "legal_name": "Same Ltd", "state_code": "27", "gstin": None})
    (entry,) = await audits(api)
    assert entry.details == {"name": {"old": "Old Name", "new": "New Name"}}        # legal_name, state, gstin unchanged
    assert (entry.actor_kind, entry.actor_id, entry.org_id, entry.target_type) == ("user", world.admin.id,
                                                                                     world.org.id, "org")


async def test_a_patch_that_changes_nothing_writes_no_audit(api):
    world = await api.seed_org(name="Same Name")
    r = await api.client.patch(url(world), json={"name": "Same Name"}, headers=api.h(world.owner))
    assert r.status_code == 200 and await audits(api) == []


async def test_a_field_can_be_cleared(api):
    world = await api.seed_org(legal_name="To Clear Ltd")
    r = await api.client.patch(url(world), json={"legal_name": None}, headers=api.h(world.owner))
    assert r.status_code == 200 and r.json()["legal_name"] is None
    assert (await audits(api))[0].details == {"legal_name": {"old": "To Clear Ltd", "new": None}}


@pytest.mark.parametrize("body", [{}, None])
async def test_an_empty_body_is_422(api, body):
    world = await api.seed_org()
    r = await api.client.patch(url(world), json=body if body is not None else {}, headers=api.h(world.owner))
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    assert await audits(api) == []


@pytest.mark.parametrize("body", [
    {"name": "ab"}, {"name": "x" * 121}, {"name": "   "}, {"billing_email": "not-an-email"}, {"billing_email": ""},
    {"gstin": "NOTAGSTIN"}, {"gstin": "09AAAAA0000A1Z"}, {"gstin": "00AAAAA0000A1Z5"},
    {"state_code": "00"}, {"state_code": "25"}, {"state_code": "9"}, {"state_code": "ZZ"},
    {"slug": "new-slug"}, {"status": "closed"}, {"type": "company"}, {"unknown": 1},
])
async def test_bad_input_is_422_and_changes_nothing(api, body):
    world = await api.seed_org()
    r = await api.client.patch(url(world), json=body, headers=api.h(world.owner))
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    saved = await row(api, world)
    assert (saved.name, saved.slug, saved.status, saved.type) == (world.org.name, world.org.slug, "active", "college")
    assert await audits(api) == []


@pytest.mark.parametrize("gstin, state, ok", [
    (GSTIN, "09", True), (GSTIN, "27", False), ("27AAAAA0000A1Z5", "27", True), ("27AAAAA0000A1Z5", "09", False),
])
async def test_the_gstin_and_state_code_must_agree_when_both_are_set(api, gstin, state, ok):
    world = await api.seed_org()
    r = await api.client.patch(url(world), json={"gstin": gstin, "state_code": state}, headers=api.h(world.owner))
    assert r.status_code == (200 if ok else 422)
    if not ok:
        assert "state_code" in r.json()["detail"]


async def test_the_check_uses_the_value_already_stored_for_the_other_field(api):
    world = await api.seed_org(state_code="27")
    bad = await api.client.patch(url(world), json={"gstin": GSTIN}, headers=api.h(world.owner))   # state 27, gstin 09...
    assert bad.status_code == 422
    good = await api.client.patch(url(world), json={"gstin": "27AAAAA0000A1Z5"}, headers=api.h(world.owner))
    assert good.status_code == 200


async def test_setting_only_one_of_them_when_the_other_is_empty_is_fine(api):
    world = await api.seed_org()
    assert (await api.client.patch(url(world), json={"gstin": GSTIN.lower()}, headers=api.h(world.owner))
            ).json()["gstin"] == GSTIN                                          # normalised to upper case
    assert (await api.client.patch(url(world), json={"gstin": None, "state_code": "33"},
                                   headers=api.h(world.owner))).status_code == 200


async def test_an_unrelated_edit_is_not_blocked_by_an_old_mismatch(api):
    world = await api.seed_org(gstin=GSTIN, state_code="27")                     # legacy data that disagrees
    r = await api.client.patch(url(world), json={"name": "Renamed Org"}, headers=api.h(world.owner))
    assert r.status_code == 200


@pytest.mark.parametrize("status, allowed", [("pilot", True), ("active", True), ("suspended", True),
                                             ("pilot_expired", True), ("closed", False)])
async def test_the_status_gate_for_edits(api, status, allowed):
    world = await api.seed_org(status=status)
    r = await api.client.patch(url(world), json={"name": "Edited Name"}, headers=api.h(world.admin))
    if allowed:
        assert r.status_code == 200
    else:
        assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"
        assert (await row(api, world)).name == world.org.name and await audits(api) == []
