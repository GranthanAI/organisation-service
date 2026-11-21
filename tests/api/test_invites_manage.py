"""List, resend and revoke invitations (org routes and the staff resend)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.invite import Invite
from app.models.job import Job
from app.security.tokens import invite_token
from tests.support.factories import NOW
from tests.support.invites import seed_invite
from tests.support.tokens import bearer, make_token


def base(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/invites"


async def get(api, world, who, **params):
    return await api.client.get(base(world), params=params, headers=api.h(who))


async def audits(api, action):
    async with api.sf() as s:
        return [a for a in (await s.execute(select(AuditLog))).scalars() if a.action == action]


async def send_jobs(api):
    async with api.sf() as s:
        return [j for j in (await s.execute(select(Job).order_by(Job.run_at))).scalars() if j.kind == "invite.send"]


async def row(api, invite) -> Invite:
    async with api.sf() as s:
        return await s.get(Invite, invite.id)


# ---- list ----

async def test_the_list_computes_status_and_never_shows_the_hash(api):
    world = await api.seed_org()
    open_, _ = await seed_invite(api.sf, world.org.id, email="open@amity.edu")
    late, _ = await seed_invite(api.sf, world.org.id, email="late@amity.edu", expires_in=-timedelta(hours=1))
    gone, _ = await seed_invite(api.sf, world.org.id, email="gone@amity.edu", revoked_at=NOW, revoke_reason="revoked")
    done, _ = await seed_invite(api.sf, world.org.id, email="done@amity.edu", accepted_at=NOW, accepted_by=world.members[0].id)
    r = await get(api, world, world.admin)
    assert r.status_code == 200
    by_email = {i["email"]: i for i in r.json()["items"]}
    assert {e: i["status"] for e, i in by_email.items()} == {"open@amity.edu": "open", "late@amity.edu": "expired",
                                                             "gone@amity.edu": "revoked", "done@amity.edu": "accepted"}
    assert by_email["done@amity.edu"]["accepted_by"] == str(world.members[0].id)
    assert set(by_email["open@amity.edu"]) == {"invite_id", "email", "role", "status", "created_by", "created_at", "expires_at",
                                              "last_sent_at", "resend_count", "accepted_by", "accepted_at"}
    assert "token" not in r.text and open_.token_hash not in r.text


async def test_filter_by_status(api):
    world = await api.seed_org()
    await seed_invite(api.sf, world.org.id, email="open@amity.edu")
    await seed_invite(api.sf, world.org.id, email="late@amity.edu", expires_in=-timedelta(hours=1))
    await seed_invite(api.sf, world.org.id, email="gone@amity.edu", revoked_at=NOW, revoke_reason="revoked")
    await seed_invite(api.sf, world.org.id, email="late2@amity.edu", expires_in=-timedelta(days=2), revoked_at=NOW, revoke_reason="expired")
    for status, emails in {"open": {"open@amity.edu"}, "revoked": {"gone@amity.edu"},
                           "expired": {"late@amity.edu", "late2@amity.edu"}, "accepted": set()}.items():
        got = {i["email"] for i in (await get(api, world, world.admin, status=status)).json()["items"]}
        assert got == emails, status
    assert (await get(api, world, world.admin, status="nonsense")).status_code == 422


async def test_newest_first_with_stable_keyset_paging(api):
    world = await api.seed_org()
    made = []
    for i in range(7):
        invite, _ = await seed_invite(api.sf, world.org.id, email=f"p{i}@amity.edu", created_at=NOW - timedelta(minutes=i))
        made.append(invite.id)
    first = (await get(api, world, world.admin, limit=3)).json()
    assert [i["invite_id"] for i in first["items"]] == [str(x) for x in made[:3]] and first["next_cursor"]
    second = (await get(api, world, world.admin, limit=3, cursor=first["next_cursor"])).json()
    third = (await get(api, world, world.admin, limit=3, cursor=second["next_cursor"])).json()
    got = [i["invite_id"] for p in (first, second, third) for i in p["items"]]
    assert got == [str(x) for x in made] and third["next_cursor"] is None
    assert (await get(api, world, world.admin, limit=0)).status_code == 422
    assert (await get(api, world, world.admin, cursor="garbage")).status_code in (400, 422)


async def test_only_this_orgs_invites_are_listed(api):
    world, other = await api.seed_org(), await api.seed_org()
    await seed_invite(api.sf, other.org.id, email="theirs@amity.edu")
    assert (await get(api, world, world.admin)).json()["items"] == []


async def test_who_may_list(api):
    world = await api.seed_org()
    assert (await get(api, world, world.owner)).status_code == 200
    assert (await get(api, world, world.admin)).status_code == 200
    assert (await get(api, world, world.members[0])).status_code == 403
    outsider = await api.seed_org()
    assert (await get(api, world, outsider.owner)).status_code in (403, 404)


# ---- resend ----

async def test_resend_kills_the_old_link_issues_a_new_one_and_queues_the_email(api):
    world = await api.seed_org()
    invite, old = await seed_invite(api.sf, world.org.id, email="new@amity.edu", created_by=world.admin.id,
                                    expires_in=timedelta(days=1))
    r = await api.post(f"{base(world)}/{invite.id}/resend", headers=api.h(world.admin))
    assert r.status_code == 200 and r.json() == {"email": "new@amity.edu", "result": "resent", "invite_id": str(invite.id)}
    saved = await row(api, invite)
    assert (saved.token_version, saved.resend_count) == (2, 1)
    assert saved.expires_at == NOW + timedelta(days=settings.INVITE_TTL_DAYS)
    preview = "/org/v1/invites/preview"
    assert (await api.client.get(preview, params={"token": old})).status_code == 404
    assert (await api.client.get(preview, params={"token": invite_token(invite.id, 2)})).status_code == 200
    (job,) = await send_jobs(api)
    assert job.payload == {"invite_id": str(invite.id), "token_version": 2}
    (audit,) = await audits(api, "invite.resent")
    assert audit.actor_id == world.admin.id and audit.target_id == str(invite.id)
    assert (await api.run_jobs()) >= 1 and api.mail.tokens() == [invite_token(invite.id, 2)]


async def test_at_most_three_resends(api):
    world = await api.seed_org()
    invite, _ = await seed_invite(api.sf, world.org.id)
    for _ in range(3):
        assert (await api.post(f"{base(world)}/{invite.id}/resend", headers=api.h(world.admin))).status_code == 200
    r = await api.post(f"{base(world)}/{invite.id}/resend", headers=api.h(world.admin))
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and int(r.headers["retry-after"]) > 0
    assert (await row(api, invite)).resend_count == 3


@pytest.mark.parametrize("state", ["revoked", "accepted"])
async def test_a_closed_invite_cannot_be_resent(api, state):
    world = await api.seed_org()
    kw = dict(revoked_at=NOW, revoke_reason="revoked") if state == "revoked" else dict(accepted_at=NOW, accepted_by=uuid.uuid4())
    invite, _ = await seed_invite(api.sf, world.org.id, **kw)
    r = await api.post(f"{base(world)}/{invite.id}/resend", headers=api.h(world.admin))
    assert r.status_code == 404 and r.json()["code"] == "INVITE_INVALID"
    assert await send_jobs(api) == []


async def test_resend_revives_an_invite_that_is_merely_past_its_expiry(api):
    world = await api.seed_org()
    invite, _ = await seed_invite(api.sf, world.org.id, expires_in=-timedelta(days=1))
    assert (await api.post(f"{base(world)}/{invite.id}/resend", headers=api.h(world.admin))).status_code == 200
    assert (await get(api, world, world.admin)).json()["items"][0]["status"] == "open"


async def test_resend_in_an_inactive_org_is_refused(api):
    world = await api.seed_org(status="suspended")
    invite, _ = await seed_invite(api.sf, world.org.id)
    r = await api.post(f"{base(world)}/{invite.id}/resend", headers=api.h(world.owner))
    assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"


async def test_resend_unknown_or_other_orgs_invite_is_404(api):
    world, other = await api.seed_org(), await api.seed_org()
    theirs, _ = await seed_invite(api.sf, other.org.id)
    for invite_id in (uuid.uuid4(), theirs.id):
        r = await api.post(f"{base(world)}/{invite_id}/resend", headers=api.h(world.admin))
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert (await row(api, theirs)).resend_count == 0


async def test_a_member_cannot_resend_or_revoke(api):
    world = await api.seed_org()
    invite, _ = await seed_invite(api.sf, world.org.id)
    assert (await api.post(f"{base(world)}/{invite.id}/resend", headers=api.h(world.members[0]))).status_code == 403
    assert (await api.client.delete(f"{base(world)}/{invite.id}", headers={**api.h(world.members[0]), "Idempotency-Key": "k" * 10})).status_code == 403


# ---- revoke ----

async def test_revoke_closes_the_invite_and_audits_it(api):
    world = await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id, email="x@amity.edu")
    r = await api.client.delete(f"{base(world)}/{invite.id}", headers={**api.h(world.admin), "Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 204 and r.content == b""
    saved = await row(api, invite)
    assert (saved.revoked_at, saved.revoke_reason) == (NOW, "revoked")
    assert (await api.client.get("/org/v1/invites/preview", params={"token": token})).status_code == 404
    (audit,) = await audits(api, "invite.revoked")
    assert audit.actor_id == world.admin.id and audit.details["email"] == "x@amity.edu"
    assert (await get(api, world, world.admin, status="revoked")).json()["items"][0]["status"] == "revoked"


async def test_revoking_an_already_closed_invite_is_a_quiet_204(api):
    world = await api.seed_org()
    gone, _ = await seed_invite(api.sf, world.org.id, email="a@amity.edu", revoked_at=NOW - timedelta(days=1), revoke_reason="revoked")
    done, _ = await seed_invite(api.sf, world.org.id, email="b@amity.edu", accepted_at=NOW, accepted_by=uuid.uuid4())
    for invite in (gone, done):
        r = await api.client.delete(f"{base(world)}/{invite.id}", headers={**api.h(world.admin), "Idempotency-Key": uuid.uuid4().hex})
        assert r.status_code == 204
    assert await audits(api, "invite.revoked") == []
    assert (await row(api, gone)).revoked_at == NOW - timedelta(days=1)          # not overwritten
    assert (await row(api, done)).revoked_at is None                              # an accepted invite is not revoked


async def test_revoking_an_unknown_invite_is_404(api):
    world = await api.seed_org()
    r = await api.client.delete(f"{base(world)}/{uuid.uuid4()}", headers={**api.h(world.admin), "Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 404


# ---- staff resend ----

STAFF = lambda: bearer(make_token(roles=["admin", "billing_admin", "support"], email="staff@granthan.com"))   # noqa: E731


async def test_staff_can_resend_with_a_reason(api):
    world = await api.seed_org()
    invite, old = await seed_invite(api.sf, world.org.id, email="x@amity.edu", created_by=world.admin.id)
    url = f"/admin/org/v1/orgs/{world.org.id}/invites/{invite.id}/resend"
    assert (await api.post(url, {}, STAFF())).status_code == 422                        # reason required
    r = await api.post(url, {"reason": "customer asked us to resend"}, STAFF())
    assert r.status_code == 200 and r.json()["result"] == "resent"
    saved = await row(api, invite)
    assert saved.token_version == 2 and saved.resend_count == 1
    assert (await api.client.get("/org/v1/invites/preview", params={"token": old})).status_code == 404
    (audit,) = await audits(api, "invite.resent")
    assert audit.actor_kind == "staff" and audit.reason == "customer asked us to resend"
    assert len(await send_jobs(api)) == 1


async def test_staff_resend_needs_a_staff_token(api):
    world = await api.seed_org()
    invite, _ = await seed_invite(api.sf, world.org.id)
    url = f"/admin/org/v1/orgs/{world.org.id}/invites/{invite.id}/resend"
    assert (await api.post(url, {"reason": "because I can"}, api.h(world.owner))).status_code == 403
    assert (await api.client.post(url, json={"reason": "no token"}, headers={"Idempotency-Key": "k" * 10})).status_code == 401
