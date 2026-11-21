"""/org/v1/orgs/{org_id}: members list, role changes, removal, leave, transfer; the status gate; idempotency."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from tests.support.factories import NOW, make_membership


def base(world) -> str:
    return f"/org/v1/orgs/{world.org.id}"


async def audit_actions(api) -> list[str]:
    async with api.sf() as s:
        return [a.action for a in (await s.execute(select(AuditLog).order_by(AuditLog.at, AuditLog.id))).scalars()]


async def audit_rows(api, action=None) -> list[AuditLog]:
    async with api.sf() as s:
        rows = list((await s.execute(select(AuditLog).order_by(AuditLog.at, AuditLog.id))).scalars())
    return [a for a in rows if action is None or a.action == action]


async def outbox(api, topic="org.member.left") -> list[OutboxEvent]:
    async with api.sf() as s:
        return [e for e in (await s.execute(select(OutboxEvent))).scalars() if e.topic == topic]


async def membership(api, world, person) -> Membership:
    async with api.sf() as s:
        return (await s.execute(select(Membership).where(
            Membership.org_id == world.org.id, Membership.user_id == person.id))).scalar_one()


async def page(api, world, headers, **params):
    return await api.client.get(f"{base(world)}/members", params=params, headers=headers)


# ================================================================ the list
async def test_120_members_page_correctly_at_50(api):
    world = await api.seed_org(admins=1, members=118, seats=200)
    seen, cursor, sizes = [], None, []
    for _ in range(5):
        r = await page(api, world, api.h(world.owner), limit=50, **({"cursor": cursor} if cursor else {}))
        assert r.status_code == 200
        body = r.json()
        sizes.append(len(body["items"]))
        seen += [m["user_id"] for m in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sizes == [50, 50, 20] and len(seen) == 120 == len(set(seen))
    assert seen[0] == str(world.owner.id)                                          # oldest first
    assert body["next_cursor"] is None


async def test_each_row_has_the_documented_shape(api):
    world = await api.seed_org(members=1)
    async with unit_of_work(api.sf) as s:                                          # a member who is not a known user yet
        await __import__("app.repositories.membership_repository", fromlist=["x"]).MembershipRepository(s).add(
            make_membership(world.org.id, uuid.uuid4(), joined_at=NOW + timedelta(minutes=5)))
    rows = (await page(api, world, api.h(world.admin))).json()["items"]
    assert set(rows[0]) == {"user_id", "name", "email", "role", "source", "joined_at"}
    owner_row = rows[0]
    assert (owner_row["user_id"], owner_row["name"], owner_row["email"], owner_row["role"], owner_row["source"]) == (
        str(world.owner.id), world.owner.name, world.owner.email, "owner", "staff")
    assert rows[-1]["name"] is None and rows[-1]["email"] is None and rows[-1]["role"] == "member"


async def test_paging_stays_stable_while_members_join_between_pages(api):
    world = await api.seed_org(admins=1, members=10, seats=100)                    # 12 people
    first = (await page(api, world, api.h(world.owner), limit=5)).json()
    async with unit_of_work(api.sf) as s:                                          # three people join between the requests
        joiners = [await api.person(s, world.org.id, "member", 1000 + i) for i in range(3)]
    ids = [m["user_id"] for m in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        nxt = (await page(api, world, api.h(world.owner), limit=5, cursor=cursor)).json()
        ids += [m["user_id"] for m in nxt["items"]]
        cursor = nxt["next_cursor"]
    assert len(ids) == len(set(ids))                                                # no row twice
    original = {str(world.owner.id), str(world.admin.id), *(str(m.id) for m in world.members)}
    assert original <= set(ids)                                                     # none missed
    assert {str(j.id) for j in joiners} <= set(ids)                                 # and the joiners sort after, so they show up


async def test_a_member_leaving_between_pages_does_not_skip_or_repeat_anyone(api):
    world = await api.seed_org(admins=0, members=9, seats=100)                     # 10 people
    first = (await page(api, world, api.h(world.owner), limit=4)).json()
    async with unit_of_work(api.sf) as s:
        row = (await s.execute(select(Membership).where(Membership.user_id == world.members[0].id))).scalar_one()
        row.status, row.ended_at, row.end_reason = "left", NOW, "left"
    ids = [m["user_id"] for m in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        nxt = (await page(api, world, api.h(world.owner), limit=4, cursor=cursor)).json()
        ids += [m["user_id"] for m in nxt["items"]]
        cursor = nxt["next_cursor"]
    assert len(ids) == len(set(ids))
    assert {str(m.id) for m in world.members[1:]} <= set(ids)


@pytest.mark.parametrize("limit, status", [(1, 200), (100, 200), (101, 422), (0, 422), (-1, 422)])
async def test_the_limit_is_capped_at_100(api, limit, status):
    world = await api.seed_org()
    assert (await page(api, world, api.h(world.owner), limit=limit)).status_code == status


@pytest.mark.parametrize("cursor", ["garbage", "!!!", "YWJj", "", " "])
async def test_a_bad_cursor_is_422(api, cursor):
    world = await api.seed_org()
    r = await page(api, world, api.h(world.owner), cursor=cursor) if cursor.strip() else \
        await page(api, world, api.h(world.owner), cursor="bad-cursor")
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


async def test_search_by_email_and_by_name_case_insensitive(api):
    world = await api.seed_org(admins=0, members=0)
    async with unit_of_work(api.sf) as s:
        ada = await api.person(s, world.org.id, "member", 10, name="Ada Lovelace")
        await api.person(s, world.org.id, "member", 11, name="Grace Hopper")
    by_name = (await page(api, world, api.h(world.owner), q="LOVELACE")).json()["items"]
    assert [m["name"] for m in by_name] == ["Ada Lovelace"]
    by_email = (await page(api, world, api.h(world.owner), q=ada.email.upper())).json()["items"]
    assert [m["user_id"] for m in by_email] == [str(ada.id)]
    assert (await page(api, world, api.h(world.owner), q="nobody-called-this")).json()["items"] == []
    assert len((await page(api, world, api.h(world.owner), q="amity.edu")).json()["items"]) == 3      # every email matches


@pytest.mark.parametrize("q", ["a", "x" * 101])
async def test_q_must_be_2_to_100_characters(api, q):
    world = await api.seed_org()
    assert (await page(api, world, api.h(world.owner), q=q)).status_code == 422


async def test_role_filter(api):
    world = await api.seed_org(admins=2, members=3)
    admins = (await page(api, world, api.h(world.owner), role="admin")).json()["items"]
    assert {m["role"] for m in admins} == {"admin"} and len(admins) == 2
    assert [m["role"] for m in (await page(api, world, api.h(world.owner), role="owner")).json()["items"]] == ["owner"]
    assert (await page(api, world, api.h(world.owner), role="boss")).status_code == 422


async def test_only_active_members_are_listed_and_a_member_cannot_list(api):
    world = await api.seed_org(members=2)
    async with unit_of_work(api.sf) as s:
        await api.person(s, world.org.id, "member", 90, status="left")
    assert len((await page(api, world, api.h(world.owner))).json()["items"]) == 4
    r = await page(api, world, api.h(world.member))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    assert len((await page(api, world, api.h(world.admin))).json()["items"]) == 4


# ================================================================ remove
async def test_an_admin_removes_a_member_and_org_member_left_is_emitted(api):
    world = await api.seed_org()
    r = await api.client.delete(f"{base(world)}/members/{world.member.id}", headers=api.h(world.admin))
    assert r.status_code == 204 and r.content == b""
    row = await membership(api, world, world.member)
    assert (row.status, row.end_reason, row.ended_at) == ("removed", "removed", NOW)
    (event,) = await outbox(api)
    assert event.key == str(world.member.id) and event.payload["reason"] == "removed"
    assert event.payload["org_type"] == "college" and event.payload["role"] == "member"
    assert await audit_actions(api) == ["member.removed"]                           # exactly one audit row
    (entry,) = await audit_rows(api)
    assert (entry.actor_kind, entry.actor_id, entry.org_id) == ("user", world.admin.id, world.org.id)
    assert entry.details["reason"] == "removed"


async def test_an_admin_cannot_remove_an_admin_or_the_owner_but_an_owner_can_remove_an_admin(api):
    world = await api.seed_org(admins=2)
    other_admin = world.admins[1]
    for target in (other_admin, world.owner):
        r = await api.client.delete(f"{base(world)}/members/{target.id}", headers=api.h(world.admin))
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    assert (await membership(api, world, other_admin)).status == "active"
    assert await audit_actions(api) == [] and await outbox(api) == []
    assert (await api.client.delete(f"{base(world)}/members/{other_admin.id}", headers=api.h(world.owner))
            ).status_code == 204


async def test_a_member_cannot_remove_anyone(api):
    world = await api.seed_org(members=2)
    r = await api.client.delete(f"{base(world)}/members/{world.members[1].id}", headers=api.h(world.member))
    assert r.status_code == 403


@pytest.mark.parametrize("who", ["owner", "admin"])
async def test_nobody_can_remove_themselves(api, who):
    world = await api.seed_org()
    person = getattr(world, who)
    r = await api.client.delete(f"{base(world)}/members/{person.id}", headers=api.h(person))
    assert r.status_code == 422 and "leave" in r.json()["detail"]
    assert (await membership(api, world, person)).status == "active"


async def test_removing_an_unknown_or_already_removed_member_is_404(api):
    world = await api.seed_org()
    assert (await api.client.delete(f"{base(world)}/members/{uuid.uuid4()}", headers=api.h(world.owner))).status_code == 404
    assert (await api.client.delete(f"{base(world)}/members/{world.member.id}", headers=api.h(world.owner))).status_code == 204
    assert (await api.client.delete(f"{base(world)}/members/{world.member.id}", headers=api.h(world.owner))).status_code == 404


async def test_a_member_of_another_org_cannot_be_removed_through_this_one(api):
    world, other = await api.seed_org(), await api.seed_org()
    r = await api.client.delete(f"{base(world)}/members/{other.member.id}", headers=api.h(world.owner))
    assert r.status_code == 404
    assert (await membership(api, other, other.member)).status == "active"


# ================================================================ change role
async def test_the_owner_promotes_and_demotes_with_one_audit_row_each_and_no_event(api):
    world = await api.seed_org()
    r = await api.client.patch(f"{base(world)}/members/{world.member.id}", json={"role": "admin"}, headers=api.h(world.owner))
    assert r.status_code == 200 and r.json() == {"user_id": str(world.member.id), "role": "admin"}
    r = await api.client.patch(f"{base(world)}/members/{world.admin.id}", json={"role": "member"}, headers=api.h(world.owner))
    assert r.json()["role"] == "member"
    changes = await audit_rows(api, "member.role_changed")
    assert len(changes) == 2 == len(await audit_actions(api))
    assert changes[0].details == {"user_id": str(world.member.id), "from": "member", "to": "admin"}
    assert changes[1].details == {"user_id": str(world.admin.id), "from": "admin", "to": "member"}
    assert {c.actor_kind for c in changes} == {"user"} and {c.actor_id for c in changes} == {world.owner.id}
    assert await outbox(api, "org.member.left") == [] and await outbox(api, "org.member.joined") == []   # no Kafka event


async def test_the_same_role_is_a_no_op_with_no_audit(api):
    world = await api.seed_org()
    r = await api.client.patch(f"{base(world)}/members/{world.member.id}", json={"role": "member"}, headers=api.h(world.owner))
    assert r.status_code == 200 and r.json()["role"] == "member" and await audit_actions(api) == []


async def test_nobody_can_set_owner_here(api):
    world = await api.seed_org()
    r = await api.client.patch(f"{base(world)}/members/{world.admin.id}", json={"role": "owner"}, headers=api.h(world.owner))
    assert r.status_code == 422 and "transfer" in r.json()["detail"]
    assert (await membership(api, world, world.admin)).role == "admin"


async def test_you_cannot_change_your_own_role_and_an_owner_cannot_be_changed(api):
    world = await api.seed_org()
    own = await api.client.patch(f"{base(world)}/members/{world.owner.id}", json={"role": "admin"}, headers=api.h(world.owner))
    assert own.status_code == 403 and own.json()["code"] == "FORBIDDEN_ROLE"
    async with unit_of_work(api.sf) as s:
        second_owner = await api.person(s, world.org.id, "owner", 70)
    r = await api.client.patch(f"{base(world)}/members/{second_owner.id}", json={"role": "member"}, headers=api.h(world.owner))
    assert r.status_code == 422 and "transfer" in r.json()["detail"]
    assert (await membership(api, world, second_owner)).role == "owner" and await audit_actions(api) == []


@pytest.mark.parametrize("who", ["admin", "member"])
async def test_admins_and_members_cannot_change_roles(api, who):
    world = await api.seed_org()
    r = await api.client.patch(f"{base(world)}/members/{world.members[1].id}", json={"role": "admin"},
                               headers=api.h(getattr(world, who)))
    assert r.status_code == 403


@pytest.mark.parametrize("body", [{"role": "boss"}, {"role": ""}, {}, {"role": "admin", "extra": 1}])
async def test_a_bad_role_body_is_422(api, body):
    world = await api.seed_org()
    r = await api.client.patch(f"{base(world)}/members/{world.member.id}", json=body, headers=api.h(world.owner))
    assert r.status_code == 422


async def test_changing_the_role_of_a_non_member_is_404(api):
    world = await api.seed_org()
    assert (await api.client.patch(f"{base(world)}/members/{uuid.uuid4()}", json={"role": "admin"},
                                   headers=api.h(world.owner))).status_code == 404


# ================================================================ leave
async def test_a_member_leaves_and_then_has_no_org(api):
    world = await api.seed_org()
    r = await api.post(f"{base(world)}/leave", headers=api.h(world.member))
    assert r.status_code == 200 and r.json() == {"left": True}
    row = await membership(api, world, world.member)
    assert (row.status, row.end_reason) == ("left", "left")
    (event,) = await outbox(api)
    assert event.payload["reason"] == "left" and event.key == str(world.member.id)
    assert await audit_actions(api) == ["member.left"]
    assert (await audit_rows(api))[0].actor_kind == "user"
    assert (await api.client.get("/org/v1/me", headers=api.h(world.member))).status_code == 404       # no longer in an org
    assert (await api.post(f"{base(world)}/leave", headers=api.h(world.member))).status_code == 404   # and not a member now


async def test_the_last_owner_cannot_leave_but_a_second_owner_can(api):
    world = await api.seed_org()
    r = await api.post(f"{base(world)}/leave", headers=api.h(world.owner))
    assert r.status_code == 409 and r.json()["code"] == "LAST_OWNER"
    assert (await membership(api, world, world.owner)).status == "active" and await audit_actions(api) == []
    async with unit_of_work(api.sf) as s:
        second = await api.person(s, world.org.id, "owner", 70)
    assert (await api.post(f"{base(world)}/leave", headers=api.h(second))).status_code == 200
    assert (await api.post(f"{base(world)}/leave", headers=api.h(world.owner))).status_code == 409


# ================================================================ transfer
async def test_transfer_ownership_happy_path(api):
    world = await api.seed_org()
    r = await api.post(f"{base(world)}/transfer-ownership", {"to_user_id": str(world.admin.id)}, api.h(world.owner))
    assert r.status_code == 200
    assert r.json() == {"owner_id": str(world.admin.id), "previous_owner_id": str(world.owner.id),
                        "previous_owner_role": "admin"}
    assert (await membership(api, world, world.admin)).role == "owner"
    assert (await membership(api, world, world.owner)).role == "admin"
    assert sorted(await audit_actions(api)) == ["member.role_changed", "member.role_changed", "org.ownership_transferred"]
    changes = {a.details["user_id"]: a.details for a in await audit_rows(api, "member.role_changed")}
    assert changes[str(world.admin.id)]["from"] == "admin" and changes[str(world.admin.id)]["to"] == "owner"
    assert changes[str(world.owner.id)]["from"] == "owner" and changes[str(world.owner.id)]["to"] == "admin"
    (moved,) = await audit_rows(api, "org.ownership_transferred")
    assert moved.details == {"from_user_id": str(world.owner.id), "to_user_id": str(world.admin.id)}
    assert {a.actor_kind for a in await audit_rows(api)} == {"user"}
    # the old owner is now an admin: they may leave, the new owner is not the last-owner problem
    assert (await api.post(f"{base(world)}/leave", headers=api.h(world.owner))).status_code == 200


@pytest.mark.parametrize("target, status", [("member", 422), ("self", 422), ("stranger", 422), ("ended", 422)])
async def test_transfer_refusals(api, target, status):
    world = await api.seed_org()
    async with unit_of_work(api.sf) as s:
        ended = await api.person(s, world.org.id, "admin", 80, status="left")
    to = {"member": world.member.id, "self": world.owner.id, "stranger": uuid.uuid4(), "ended": ended.id}[target]
    r = await api.post(f"{base(world)}/transfer-ownership", {"to_user_id": str(to)}, api.h(world.owner))
    assert r.status_code == status and r.json()["code"] == "VALIDATION_ERROR"
    assert (await membership(api, world, world.owner)).role == "owner" and await audit_actions(api) == []


@pytest.mark.parametrize("who", ["admin", "member"])
async def test_only_the_owner_can_transfer(api, who):
    world = await api.seed_org()
    r = await api.post(f"{base(world)}/transfer-ownership", {"to_user_id": str(world.admin.id)}, api.h(getattr(world, who)))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"


async def test_a_bad_transfer_body_is_422(api):
    world = await api.seed_org()
    for body in ({}, {"to_user_id": "nope"}, {"to_user_id": str(world.admin.id), "extra": 1}):
        assert (await api.post(f"{base(world)}/transfer-ownership", body, api.h(world.owner))).status_code == 422


# ================================================================ a non-member gets 404 everywhere
async def test_a_non_member_gets_404_on_every_route(api):
    world, other = await api.seed_org(), await api.seed_org()
    headers = api.h(other.owner)
    b = base(world)
    calls = [
        api.client.get(f"{b}/members", headers=headers),
        api.client.patch(f"{b}/members/{world.member.id}", json={"role": "admin"}, headers=headers),
        api.client.delete(f"{b}/members/{world.member.id}", headers=headers),
        api.post(f"{b}/leave", headers=headers),
        api.post(f"{b}/transfer-ownership", {"to_user_id": str(world.admin.id)}, headers),
    ]
    for call in calls:
        r = await call
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    stranger = api.h(uuid.uuid4())
    assert (await api.client.get(f"{b}/members", headers=stranger)).status_code == 404
    assert (await api.client.get(f"/org/v1/orgs/{uuid.uuid4()}/members", headers=headers)).status_code == 404
    assert await audit_actions(api) == []


# ================================================================ the status gate
@pytest.mark.parametrize("status", ["suspended", "pilot_expired"])
async def test_a_suspended_or_expired_org_still_allows_role_changes_removals_and_leave(api, status):
    world = await api.seed_org(status=status, members=4)
    b = base(world)
    assert (await api.client.patch(f"{b}/members/{world.members[0].id}", json={"role": "admin"}, headers=api.h(world.owner))
            ).status_code == 200
    assert (await api.client.delete(f"{b}/members/{world.members[1].id}", headers=api.h(world.admin))).status_code == 204
    assert (await api.post(f"{b}/leave", headers=api.h(world.members[2]))).status_code == 200
    assert (await page(api, world, api.h(world.owner))).status_code == 200                       # and reads work


async def test_a_closed_org_refuses_every_write_including_leave_but_allows_reads(api):
    world = await api.seed_org(status="closed")
    b = base(world)
    refused = [
        await api.client.patch(f"{b}/members/{world.member.id}", json={"role": "admin"}, headers=api.h(world.owner)),
        await api.client.delete(f"{b}/members/{world.member.id}", headers=api.h(world.owner)),
        await api.post(f"{b}/leave", headers=api.h(world.member)),
        await api.post(f"{b}/transfer-ownership", {"to_user_id": str(world.admin.id)}, api.h(world.owner)),
    ]
    for r in refused:
        assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"
    assert await audit_actions(api) == [] and await outbox(api) == []
    assert (await membership(api, world, world.member)).status == "active"
    assert (await page(api, world, api.h(world.owner))).status_code == 200


async def test_transfer_works_in_a_suspended_org(api):
    world = await api.seed_org(status="suspended")
    r = await api.post(f"{base(world)}/transfer-ownership", {"to_user_id": str(world.admin.id)}, api.h(world.owner))
    assert r.status_code == 200


# ================================================================ idempotency
async def test_a_replayed_leave_leaves_once(api):
    world = await api.seed_org()
    first = await api.post(f"{base(world)}/leave", headers=api.h(world.member), key="leave-key-0001")
    second = await api.post(f"{base(world)}/leave", headers=api.h(world.member), key="leave-key-0001")
    assert first.status_code == second.status_code == 200 and second.headers["idempotent-replayed"] == "true"
    assert len(await outbox(api)) == 1 and await audit_actions(api) == ["member.left"]


async def test_a_replayed_transfer_transfers_once(api):
    world = await api.seed_org()
    body = {"to_user_id": str(world.admin.id)}
    first = await api.post(f"{base(world)}/transfer-ownership", body, api.h(world.owner), key="transfer-key-01")
    second = await api.post(f"{base(world)}/transfer-ownership", body, api.h(world.owner), key="transfer-key-01")
    assert first.status_code == second.status_code == 200 and second.json() == first.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert len(await audit_rows(api, "org.ownership_transferred")) == 1


async def test_posts_need_an_idempotency_key_but_patch_and_delete_do_not(api):
    world = await api.seed_org()
    assert (await api.client.post(f"{base(world)}/leave", headers=api.h(world.member))).status_code == 422
    assert (await api.client.patch(f"{base(world)}/members/{world.member.id}", json={"role": "admin"},
                                   headers=api.h(world.owner))).status_code == 200
    assert (await api.client.delete(f"{base(world)}/members/{world.members[1].id}", headers=api.h(world.owner))
            ).status_code == 204


# ================================================================ exactly one audit row per write
async def test_every_write_leaves_exactly_one_audit_row_and_transfer_three(api):
    world = await api.seed_org(members=4, seats=50)
    b = base(world)
    steps = [
        (api.client.patch(f"{b}/members/{world.members[0].id}", json={"role": "admin"}, headers=api.h(world.owner)), 1),
        (api.client.delete(f"{b}/members/{world.members[1].id}", headers=api.h(world.owner)), 1),
        (api.client.patch(f"{b}", json={"name": "Renamed College"}, headers=api.h(world.owner)), 1),
        (api.post(f"{b}/leave", headers=api.h(world.members[2])), 1),
        (api.post(f"{b}/transfer-ownership", {"to_user_id": str(world.admin.id)}, api.h(world.owner)), 3),
    ]
    total = 0
    for call, rows in steps:
        r = await call
        assert r.status_code in (200, 204)
        total += rows
        assert len(await audit_actions(api)) == total
