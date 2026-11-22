"""Join requests: the domain match, a user's request, the admins' queue, approving, declining, and expiry."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.domain import JoinRequest, OrgDomain
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.known_user_repository import KnownUserRepository
from app.services import domain_jobs
from tests.support.factories import NOW, ensure_known
from tests.support.invites import seed_invite
from tests.support.tokens import bearer, make_token

MATCH = "/org/v1/me/domain-match"
REQUESTS = "/org/v1/join-requests"


def queue(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/join-requests"


async def verified_domain(api, world, domain="amity.edu", include_subdomains=False):
    async with unit_of_work(api.sf) as s:
        s.add(OrgDomain(org_id=world.org.id, domain=domain, verify_token="t" * 43, verified_at=NOW, verified_by=world.owner.id,
                        verify_method="dns", auto_join=False, include_subdomains=include_subdomains, failed_checks=0, added_at=NOW))


async def student(api, email="x@amity.edu", *, email_verified=True, name="Asha Student"):
    """A known user with a JWT; `email_verified` sets known_users.email_verified_at (a verified email)."""
    user_id = uuid.uuid4()
    async with unit_of_work(api.sf) as s:
        await KnownUserRepository(s).upsert(user_id, email, name, NOW)
        if email_verified:
            await KnownUserRepository(s).set_email_verified(user_id, email, NOW, NOW)
    return user_id, bearer(make_token(sub=user_id, email=email))


async def table(api, model):
    async with api.sf() as s:
        return list((await s.execute(select(model))).scalars())


async def ask(api, headers):
    return await api.post(REQUESTS, None, headers)


# ---------------------------------------------------------------- the match
async def test_domain_match_found(api):
    world = await api.seed_org(name="Amity University", type="college")
    await verified_domain(api, world)
    _, h = await student(api)
    r = await api.client.get(MATCH, headers=h)
    assert r.status_code == 200
    assert r.json() == {"org_id": str(world.org.id), "org_name": "Amity University", "org_type": "college", "can_request": True,
                        "pending_request_id": None}
    created = await ask(api, h)
    assert created.status_code == 201
    assert (await api.client.get(MATCH, headers=h)).json()["pending_request_id"] == created.json()["id"]


async def test_domain_match_follows_subdomain_rules(api):
    world = await api.seed_org()
    await verified_domain(api, world, include_subdomains=False)
    _, h = await student(api, "x@cse.amity.edu")
    assert (await api.client.get(MATCH, headers=h)).status_code == 404
    async with unit_of_work(api.sf) as s:
        (await s.get(OrgDomain, (world.org.id, "amity.edu"))).include_subdomains = True
    assert (await api.client.get(MATCH, headers=h)).status_code == 200


async def test_domain_match_is_404_when_it_should_be(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    _, unverified = await student(api, "a@amity.edu", email_verified=False)
    assert (await api.client.get(MATCH, headers=unverified)).status_code == 404                      # the email is not verified
    _, nodomain = await student(api, "b@elsewhere.org")
    assert (await api.client.get(MATCH, headers=nodomain)).status_code == 404                        # no verified domain
    _, gmail = await student(api, "c@gmail.com")
    assert (await api.client.get(MATCH, headers=gmail)).status_code == 404
    member = world.members[0]
    async with unit_of_work(api.sf) as s:                                                            # already in an org
        await KnownUserRepository(s).set_email_verified(member.id, member.email, NOW, NOW)
    assert (await api.client.get(MATCH, headers=api.h(member, email="m@amity.edu"))).status_code == 404
    assert (await api.client.get(MATCH)).status_code == 401


async def test_domain_match_ignores_an_org_that_cannot_take_members(api):
    for status in ("suspended", "pilot_expired", "closed"):
        world = await api.seed_org(status=status)
        await verified_domain(api, world, domain=f"{status.replace('_', '-')}.edu")
        _, h = await student(api, f"x@{status.replace('_', '-')}.edu")
        assert (await api.client.get(MATCH, headers=h)).status_code == 404


async def test_a_user_who_verified_before_the_domain_existed_can_find_and_request(api):
    world = await api.seed_org()
    _, h = await student(api)                                                                        # verified first
    assert (await api.client.get(MATCH, headers=h)).status_code == 404
    await verified_domain(api, world)                                                                # the org verifies its domain later
    assert (await api.client.get(MATCH, headers=h)).status_code == 200
    assert (await ask(api, h)).status_code == 201


# ---------------------------------------------------------------- the user's request
async def test_create_then_duplicate_then_cancel(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    user_id, h = await student(api)
    r = await ask(api, h)
    assert r.status_code == 201 and r.json()["org_id"] == str(world.org.id) and r.json()["status"] == "pending"
    (row,) = await table(api, JoinRequest)
    assert (row.user_id, row.org_id, row.domain, row.status, row.created_at) == (user_id, world.org.id, "amity.edu", "pending", NOW)
    (audit,) = [a for a in await table(api, AuditLog) if a.action == "join_request.created"]
    assert audit.actor_id == user_id and audit.org_id == world.org.id
    dup = await ask(api, h)
    assert dup.status_code == 409 and dup.json()["code"] == "JOIN_REQUEST_PENDING"
    cancel = await api.client.delete(f"{REQUESTS}/{r.json()['id']}", headers={**h, "Idempotency-Key": uuid.uuid4().hex})
    assert cancel.status_code == 204
    (row,) = await table(api, JoinRequest)
    assert (row.status, row.decided_by, row.decided_at) == ("declined", user_id, NOW)                 # declined by the requester
    assert (await ask(api, h)).status_code == 201                                                   # free to ask again
    again = await api.client.delete(f"{REQUESTS}/{r.json()['id']}", headers={**h, "Idempotency-Key": uuid.uuid4().hex})
    assert again.status_code == 204                                                                  # a closed request: a quiet no-op


async def test_create_refusals(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    _, nomatch = await student(api, "x@elsewhere.org")
    assert (await ask(api, nomatch)).status_code == 404
    _, unverified = await student(api, "u@amity.edu", email_verified=False)
    assert (await ask(api, unverified)).status_code == 404
    member = world.members[0]
    r = await ask(api, api.h(member, email="m@amity.edu"))
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_IN_ORG"
    assert (await api.client.post(REQUESTS, headers={"Idempotency-Key": uuid.uuid4().hex})).status_code == 401
    assert (await api.client.post(REQUESTS, headers=(await student(api, "k@amity.edu"))[1])).status_code == 422     # no Idempotency-Key
    assert await table(api, JoinRequest) == []


async def test_cancel_someone_elses_request_is_404(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    _, mine = await student(api, "a@amity.edu")
    _, theirs = await student(api, "b@amity.edu")
    request_id = (await ask(api, mine)).json()["id"]
    r = await api.client.delete(f"{REQUESTS}/{request_id}", headers={**theirs, "Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 404
    assert (await table(api, JoinRequest))[0].status == "pending"
    nothing = await api.client.delete(f"{REQUESTS}/{uuid.uuid4()}", headers={**mine, "Idempotency-Key": uuid.uuid4().hex})
    assert nothing.status_code == 404


# ---------------------------------------------------------------- the admins' queue
async def test_admin_lists_pending_requests_with_name_and_email_and_pages(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    ids = []
    for i in range(5):
        _, h = await student(api, f"s{i}@amity.edu", name=f"Student {i}")
        api.clock.advance(seconds=1)
        ids.append((await ask(api, h)).json()["id"])
    first = (await api.client.get(queue(world), params={"limit": 2}, headers=api.h(world.admin))).json()
    assert [i["id"] for i in first["items"]] == ids[:2] and first["next_cursor"]                   # oldest first: the queue order
    assert first["items"][0]["name"] == "Student 0" and first["items"][0]["email"] == "s0@amity.edu"
    assert first["items"][0]["domain"] == "amity.edu" and first["items"][0]["status"] == "pending"
    second = (await api.client.get(queue(world), params={"limit": 2, "cursor": first["next_cursor"]}, headers=api.h(world.owner))).json()
    third = (await api.client.get(queue(world), params={"limit": 2, "cursor": second["next_cursor"]}, headers=api.h(world.owner))).json()
    assert [i["id"] for p in (first, second, third) for i in p["items"]] == ids and third["next_cursor"] is None
    assert (await api.client.get(queue(world), params={"status": "approved"}, headers=api.h(world.admin))).json()["items"] == []
    assert (await api.client.get(queue(world), params={"status": "bogus"}, headers=api.h(world.admin))).status_code == 422
    assert (await api.client.get(queue(world), params={"limit": 0}, headers=api.h(world.admin))).status_code == 422


async def test_only_admins_of_this_org_may_work_the_queue(api):
    world, other = await api.seed_org(), await api.seed_org()
    await verified_domain(api, world)
    _, h = await student(api)
    request_id = (await ask(api, h)).json()["id"]
    assert (await api.client.get(queue(world), headers=api.h(world.members[0]))).status_code == 403          # no MANAGE_JOIN_REQUESTS
    assert (await api.post(f"{queue(world)}/{request_id}/approve", None, api.h(world.members[0]))).status_code == 403
    assert (await api.client.get(queue(world), headers=api.h(other.owner))).status_code == 404
    assert (await api.post(f"{queue(other)}/{request_id}/approve", None, api.h(other.owner))).status_code == 404   # not their request
    assert (await api.client.get(queue(world))).status_code == 401


async def test_approve_joins_with_source_domain_and_closes_the_request(api):
    world = await api.seed_org(name="Amity University")
    await verified_domain(api, world)
    user_id, h = await student(api)
    request_id = (await ask(api, h)).json()["id"]
    r = await api.post(f"{queue(world)}/{request_id}/approve", None, api.h(world.admin))
    assert r.status_code == 200 and r.json() == {"id": request_id, "status": "approved"}
    (member,) = [m for m in await table(api, Membership) if m.user_id == user_id]
    assert (member.org_id, member.role, member.source, member.status) == (world.org.id, "member", "domain", "active")
    (row,) = await table(api, JoinRequest)
    assert (row.status, row.decided_by, row.decided_at) == ("approved", world.admin.id, NOW)
    (event,) = [e for e in await table(api, OutboxEvent) if e.topic == "org.member.joined" and e.key == str(user_id)]
    assert event.payload["source"] == "domain"
    approved = [a for a in await table(api, AuditLog) if a.action == "join_request.approved"]
    assert len(approved) == 1 and approved[0].actor_id == world.admin.id
    assert (await api.client.get("/org/v1/me", headers=h)).json()["org"]["id"] == str(world.org.id)
    again = await api.post(f"{queue(world)}/{request_id}/approve", None, api.h(world.admin))
    assert again.status_code == 422                                                                  # no longer pending


async def test_approve_with_no_seat_keeps_the_request_pending_and_passes_the_error_through(api):
    world = await api.seed_org(seats=5, admins=1, members=3)                                          # 5 of 5
    await verified_domain(api, world)
    user_id, h = await student(api)
    request_id = (await ask(api, h)).json()["id"]
    r = await api.post(f"{queue(world)}/{request_id}/approve", None, api.h(world.admin))
    assert r.status_code == 409 and r.json()["code"] == "SEATS_FULL"
    (row,) = await table(api, JoinRequest)
    assert (row.status, row.decided_by) == ("pending", None)
    assert [m for m in await table(api, Membership) if m.user_id == user_id] == []
    async with unit_of_work(api.sf) as s:                                                              # seats are raised
        from app.models.seat_state import SeatState
        (await s.get(SeatState, world.org.id)).seats_bought = 6
    ok = await api.post(f"{queue(world)}/{request_id}/approve", None, api.h(world.admin))
    assert ok.status_code == 200 and (await table(api, JoinRequest))[0].status == "approved"


async def test_decline(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    user_id, h = await student(api)
    request_id = (await ask(api, h)).json()["id"]
    r = await api.post(f"{queue(world)}/{request_id}/decline", None, api.h(world.owner))
    assert r.status_code == 200 and r.json()["status"] == "declined"
    (row,) = await table(api, JoinRequest)
    assert (row.status, row.decided_by) == ("declined", world.owner.id)
    assert [m for m in await table(api, Membership) if m.user_id == user_id] == []
    assert len([a for a in await table(api, AuditLog) if a.action == "join_request.declined"]) == 1
    assert (await api.post(f"{queue(world)}/{request_id}/decline", None, api.h(world.owner))).status_code == 422
    assert (await ask(api, h)).status_code == 201                                                      # the user may ask again


async def test_joining_by_invite_closes_the_pending_request(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    user_id, h = await student(api, "x@amity.edu")
    request_id = (await ask(api, h)).json()["id"]
    _, token = await seed_invite(api.sf, world.org.id, email="x@amity.edu", created_by=world.admin.id)
    accepted = await api.post("/org/v1/invites/accept", {"token": token}, h)
    assert accepted.status_code == 200
    (row,) = await table(api, JoinRequest)
    assert row.id == uuid.UUID(request_id) and (row.status, row.decided_by, row.decided_at) == ("declined", None, NOW)
    assert (await api.client.get(queue(world), headers=api.h(world.admin))).json()["items"] == []


async def test_joining_another_org_by_staff_closes_the_pending_request_too(api):
    world, other = await api.seed_org(), await api.seed_org()
    await verified_domain(api, world)
    user_id, h = await student(api)
    await ask(api, h)
    staff = bearer(make_token(roles=["admin", "billing_admin", "support"], email="staff@granthan.com"))
    r = await api.post(f"/admin/org/v1/orgs/{other.org.id}/members", {"user_id": str(user_id), "role": "member", "reason": "moved by support"}, staff)
    assert r.status_code == 201
    assert (await table(api, JoinRequest))[0].status == "declined"


async def test_the_expiry_job_expires_pending_requests_older_than_30_days(api):
    world = await api.seed_org()
    await verified_domain(api, world)
    async with unit_of_work(api.sf) as s:
        for i, age in enumerate((31, 29, 400)):
            await ensure_known(api.sf, uid := uuid.uuid4(), f"e{i}@amity.edu")
            s.add(JoinRequest(id=uuid.uuid4(), org_id=world.org.id, user_id=uid, domain="amity.edu", status="pending",
                              created_at=NOW - timedelta(days=age)))
        gone = uuid.uuid4()
        await ensure_known(api.sf, gone, "done@amity.edu")
        s.add(JoinRequest(id=uuid.uuid4(), org_id=world.org.id, user_id=gone, domain="amity.edu", status="approved",
                          created_at=NOW - timedelta(days=100)))
    async with unit_of_work(api.sf) as s:
        await domain_jobs.join_requests_expire(s, None, api.clock)
    by_age = {(NOW - r.created_at).days: r for r in await table(api, JoinRequest)}
    assert by_age[31].status == "expired" and by_age[400].status == "expired" and by_age[31].decided_at == NOW
    assert by_age[29].status == "pending" and by_age[100].status == "approved"
