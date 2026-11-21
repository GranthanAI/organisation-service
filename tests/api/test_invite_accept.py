"""POST /org/v1/invites/accept: the invite becomes a membership, with every join refusal passed through."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.constants import RATE_INVITE_ACCEPT
from app.models.audit_log import AuditLog
from app.models.invite import Invite
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from tests.support.factories import NOW
from tests.support.invites import seed_invite
from tests.support.tokens import bearer, make_token

ACCEPT = "/org/v1/invites/accept"


def who(email="invitee@amity.edu", user_id=None):
    user = user_id or uuid.uuid4()
    return user, bearer(make_token(sub=user, email=email))


async def accept(api, token, headers, key=None):
    return await api.post(ACCEPT, {"token": token}, headers, key=key)


async def rows(api, model):
    async with api.sf() as s:
        return list((await s.execute(select(model))).scalars())


async def invite_row(api, invite) -> Invite:
    async with api.sf() as s:
        return await s.get(Invite, invite.id)


async def test_accepting_makes_a_member_and_org_member_joined_has_source_invite(api):
    world = await api.seed_org(name="Amity University")
    invite, token = await seed_invite(api.sf, world.org.id, email="new.student@amity.edu", created_by=world.admin.id)
    user, headers = who("New.Student@Amity.edu")                                   # the JWT email is normalised
    r = await accept(api, token, headers)
    assert r.status_code == 200
    assert r.json() == {"org_id": str(world.org.id), "org_name": "Amity University", "role": "member"}

    (member,) = [m for m in await rows(api, Membership) if m.user_id == user]
    assert (member.org_id, member.role, member.status, member.source, member.invited_by) == (
        world.org.id, "member", "active", "invite", world.admin.id)
    row = await invite_row(api, invite)
    assert (row.uses, row.accepted_at, row.accepted_by) == (1, NOW, user)
    (event,) = [e for e in await rows(api, OutboxEvent) if e.topic == "org.member.joined"]
    assert event.key == str(user) and event.payload["source"] == "invite" and event.payload["org_type"] == "college"
    joined = [a for a in await rows(api, AuditLog) if a.action == "member.joined"]
    assert len(joined) == 1 and joined[0].details["invite_id"] == str(invite.id) and joined[0].actor_id == user
    me = (await api.client.get("/org/v1/me", headers=headers)).json()
    assert me["role"] == "member" and me["org"]["id"] == str(world.org.id)


async def test_an_admin_invite_makes_an_admin(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, email="boss@amity.edu", role="admin")
    r = await accept(api, token, who("boss@amity.edu")[1])
    assert r.status_code == 200 and r.json()["role"] == "admin"


async def test_the_wrong_email_is_403_and_the_invite_stays_open(api):
    world = await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id, email="right@amity.edu")
    r = await accept(api, token, who("someone.else@amity.edu")[1])
    assert r.status_code == 403 and r.json()["code"] == "INVITE_EMAIL_MISMATCH"
    row = await invite_row(api, invite)
    assert row.accepted_at is None and row.uses == 0 and row.revoked_at is None
    assert (await accept(api, token, who("right@amity.edu")[1])).status_code == 200


async def test_an_expired_invite_is_410_and_gets_closed_as_expired(api):
    world = await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id, email="late@amity.edu", expires_in=-timedelta(seconds=1))
    r = await accept(api, token, who("late@amity.edu")[1])
    assert r.status_code == 410 and r.json()["code"] == "INVITE_EXPIRED"
    row = await invite_row(api, invite)
    assert row.revoked_at == NOW and row.revoke_reason == "expired"                  # the close survived the error
    assert not [m for m in await rows(api, Membership) if m.role == "member" and m.source == "invite"]
    again = await accept(api, token, who("late@amity.edu")[1])                      # already closed as expired: still 410
    assert again.status_code == 410


async def test_the_same_user_retrying_gets_200_and_one_membership(api):
    world = await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id, email="me@amity.edu")
    user, headers = who("me@amity.edu")
    first = await accept(api, token, headers)
    second = await accept(api, token, headers)                                      # a new Idempotency-Key: a real retry
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len([m for m in await rows(api, Membership) if m.user_id == user]) == 1
    assert (await invite_row(api, invite)).uses == 1
    assert len([e for e in await rows(api, OutboxEvent) if e.topic == "org.member.joined"]) == 1


async def test_another_user_after_acceptance_gets_404(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, email="me@amity.edu")
    assert (await accept(api, token, who("me@amity.edu")[1])).status_code == 200
    other = await accept(api, token, who("me@amity.edu")[1])                       # same email, a different account
    assert other.status_code == 404 and other.json()["code"] == "INVITE_INVALID"


async def test_a_member_who_accepted_then_left_cannot_reuse_the_spent_invite(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, email="me@amity.edu")
    user, headers = who("me@amity.edu")
    await accept(api, token, headers)
    assert (await api.post(f"/org/v1/orgs/{world.org.id}/leave", headers=headers)).status_code == 200
    assert (await accept(api, token, headers)).status_code == 404                  # accepted by me, but I am no longer a member


async def test_seats_full_leaves_the_invite_open(api):
    world = await api.seed_org(plan_code="org_pro", seats=5, admins=1, members=3)     # 5 of 5
    invite, token = await seed_invite(api.sf, world.org.id, email="late@amity.edu")
    user, headers = who("late@amity.edu")
    r = await accept(api, token, headers)
    assert r.status_code == 409 and r.json()["code"] == "SEATS_FULL"
    row = await invite_row(api, invite)
    assert (row.accepted_at, row.uses, row.revoked_at) == (None, 0, None)
    assert not [m for m in await rows(api, Membership) if m.user_id == user]
    from app.db.session import unit_of_work
    from app.models.seat_state import SeatState
    async with unit_of_work(api.sf) as s:
        (await s.get(SeatState, world.org.id)).seats_bought = 6                         # a seat is bought
    assert (await accept(api, token, headers)).status_code == 200


async def test_pilot_full_passes_through(api):
    world = await api.seed_org(status="pilot", plan_code="pilot", seats=25, admins=0, members=23, pilot_ends_at=NOW + timedelta(days=9))
    _, token = await seed_invite(api.sf, world.org.id, email="twentysix@amity.edu")
    assert (await accept(api, token, who("twentysix@amity.edu")[1])).status_code == 200      # the 25th
    _, token2 = await seed_invite(api.sf, world.org.id, email="twentyseven@amity.edu")
    r = await accept(api, token2, who("twentyseven@amity.edu")[1])
    assert r.status_code == 409 and r.json()["code"] == "PILOT_FULL"


async def test_already_in_an_org_is_409_and_the_invite_stays_open(api):
    world, other = await api.seed_org(), await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id, email=other.member.email)
    r = await accept(api, token, api.h(other.member))
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_IN_ORG"
    assert (await invite_row(api, invite)).accepted_at is None


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_an_org_that_cannot_take_members_refuses_and_the_invite_stays_open(api, status):
    world = await api.seed_org(status=status)
    invite, token = await seed_invite(api.sf, world.org.id, email="x@amity.edu")
    r = await accept(api, token, who("x@amity.edu")[1])
    assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"
    assert (await invite_row(api, invite)).accepted_at is None


async def test_an_owner_invite_makes_the_owner(api):
    world = await api.seed_org(owner=False, admins=0, members=0, seats=None, seat_status="incomplete")
    invite, token = await seed_invite(api.sf, world.org.id, email="dean@amity.edu", role="owner")
    user, headers = who("dean@amity.edu")
    r = await accept(api, token, headers)
    assert r.status_code == 200 and r.json()["role"] == "owner"
    (member,) = await rows(api, Membership)
    assert (member.user_id, member.role) == (user, "owner")
    assert (await api.client.get("/org/v1/me", headers=headers)).json()["role"] == "owner"


async def test_an_owner_invite_to_an_org_that_already_has_an_owner_is_refused(api):
    world = await api.seed_org()                                                        # has an owner
    invite, token = await seed_invite(api.sf, world.org.id, email="second@amity.edu", role="owner")
    r = await accept(api, token, who("second@amity.edu")[1])
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    assert (await invite_row(api, invite)).accepted_at is None


@pytest.mark.parametrize("state", ["revoked", "purged", "unknown"])
async def test_closed_and_unknown_invites_are_404(api, state):
    world = await api.seed_org()
    kw = {"revoked": dict(revoked_at=NOW, revoke_reason="revoked"), "purged": dict(purged_at=NOW)}.get(state, {})
    _, token = await seed_invite(api.sf, world.org.id, email=None if state == "purged" else "x@amity.edu", **kw)
    if state == "unknown":
        token = "Z" * 43
    r = await accept(api, token, who("x@amity.edu")[1])
    assert r.status_code == 404 and r.json()["code"] == "INVITE_INVALID"


async def test_a_purged_invite_with_no_email_cannot_be_accepted_even_if_not_marked(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, email=None)                      # email gone, no flags
    assert (await accept(api, token, who("x@amity.edu")[1])).status_code == 403


async def test_two_concurrent_accepts_of_the_same_invite_give_one_membership(api):
    world = await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id, email="race@amity.edu")
    user, headers = who("race@amity.edu")
    await api.client.get("/org/v1/me", headers=headers)                                  # a login: the user is known (K2)
    results = await asyncio.gather(*[accept(api, token, headers) for _ in range(5)])
    assert sorted(r.status_code for r in results).count(200) == 5 or all(r.status_code in (200, 409) for r in results)
    assert len([m for m in await rows(api, Membership) if m.user_id == user]) == 1
    assert (await invite_row(api, invite)).uses == 1
    assert len([e for e in await rows(api, OutboxEvent) if e.topic == "org.member.joined"]) == 1


async def test_two_different_users_racing_for_one_invite_only_the_right_email_gets_in(api):
    world = await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id, email="right@amity.edu")
    right, right_h = who("right@amity.edu")
    wrong, wrong_h = who("wrong@amity.edu")
    results = await asyncio.gather(accept(api, token, right_h), accept(api, token, wrong_h))
    assert sorted(r.status_code for r in results) == [200, 403]
    assert len([m for m in await rows(api, Membership) if m.user_id in (right, wrong)]) == 1


async def test_validation_and_auth(api):
    assert (await api.client.post(ACCEPT, json={"token": "x" * 20}, headers={"Idempotency-Key": uuid.uuid4().hex})).status_code == 401
    _, headers = who()
    assert (await api.post(ACCEPT, {}, headers)).status_code == 422
    assert (await api.post(ACCEPT, {"token": "short"}, headers)).status_code == 422
    assert (await api.post(ACCEPT, {"token": "x" * 20, "extra": 1}, headers)).status_code == 422
    assert (await api.client.post(ACCEPT, json={"token": "x" * 20}, headers=headers)).status_code == 422     # no Idempotency-Key


async def test_a_replayed_accept_with_the_same_key_replays_the_answer(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, email="me@amity.edu")
    _, headers = who("me@amity.edu")
    first = await accept(api, token, headers, key="accept-key-0001")
    second = await accept(api, token, headers, key="accept-key-0001")
    assert first.json() == second.json() and second.headers["idempotent-replayed"] == "true"


async def test_the_accept_rate_limit_is_per_user(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, email="spam@amity.edu")
    _, headers = who("other@amity.edu")
    limit = RATE_INVITE_ACCEPT.limit
    codes = [(await accept(api, token, headers)).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [403] * limit and codes[-1] == 429                           # wrong email each time, then limited
    _, fresh = who("another.person@amity.edu")
    assert (await accept(api, token, fresh)).status_code == 403                          # a different user has their own bucket
