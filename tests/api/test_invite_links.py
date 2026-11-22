"""Invite links: create, list, view the URL again, revoke, and accept (max_uses, expiry, domain restriction)."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.domain import OrgDomain
from app.models.invite import Invite
from app.models.job import Job
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.security.tokens import invite_token, invite_token_hash
from tests.support.factories import NOW, ensure_known
from tests.support.tokens import bearer, make_token

ACCEPT = "/org/v1/invites/accept"
PREVIEW = "/org/v1/invites/preview"


def base(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/invite-links"


async def make_link(api, world, who=None, max_uses=5, days=7, domain_restricted=False):
    r = await api.post(base(world), {"max_uses": max_uses, "expires_in_days": days, "domain_restricted": domain_restricted},
                       api.h(who or world.admin))
    assert r.status_code == 200, r.text
    return r.json()


def token_of(url: str) -> str:
    return url.rsplit("/join/", 1)[1]


async def user(api, email=None):
    """A known user with a JWT (join() refuses users we have never seen)."""
    user_id = uuid.uuid4()
    email = email or f"u{user_id.hex[:10]}@amity.edu"
    await ensure_known(api.sf, user_id, email)
    return user_id, bearer(make_token(sub=user_id, email=email))


async def accept(api, token, headers, key=None):
    return await api.post(ACCEPT, {"token": token}, headers, key=key)


async def rows(api, model):
    async with api.sf() as s:
        return list((await s.execute(select(model))).scalars())


async def verify_domain(api, world, domain="amity.edu"):
    async with unit_of_work(api.sf) as s:
        s.add(OrgDomain(org_id=world.org.id, domain=domain, verify_token="t", verified_at=NOW, verified_by=world.owner.id,
                        verify_method="dns", auto_join=False))


# ---- create, list, url, revoke
async def test_create_a_link_returns_the_url_once_and_writes_no_event_or_job(api):
    world = await api.seed_org()
    body = await make_link(api, world, max_uses=40, days=10)
    assert set(body) == {"link_id", "url", "max_uses", "expires_at", "domain_restricted"}
    assert body["max_uses"] == 40 and body["domain_restricted"] is False
    assert body["expires_at"].startswith((NOW + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"))
    link_id = uuid.UUID(body["link_id"])
    assert body["url"] == f"{settings.APP_BASE_URL.rstrip('/')}/join/{invite_token(link_id, 1)}"
    (link,) = [i for i in await rows(api, Invite) if i.kind == "link"]
    assert (link.id, link.email, link.role, link.max_uses, link.uses, link.token_version) == (link_id, None, "member", 40, 0, 1)
    assert link.token_hash == invite_token_hash(link_id, 1) and link.created_by == world.admin.id
    assert body["url"] not in str(link.__dict__)                                           # never stored
    assert [e for e in await rows(api, OutboxEvent) if e.topic == "org.invite.created"] == []
    assert [j for j in await rows(api, Job) if j.kind == "invite.send"] == []
    (audit,) = [a for a in await rows(api, AuditLog) if a.action == "invite_link.created"]
    assert audit.actor_id == world.admin.id and audit.details["max_uses"] == 40 and body["url"] not in str(audit.details)


@pytest.mark.parametrize("body", [
    {"max_uses": 0, "expires_in_days": 7}, {"max_uses": 5001, "expires_in_days": 7}, {"max_uses": 5, "expires_in_days": 0},
    {"max_uses": 5, "expires_in_days": 31}, {"max_uses": 5}, {"expires_in_days": 7}, {"max_uses": 5, "expires_in_days": 7, "role": "admin"}])
async def test_create_validation(api, body):
    world = await api.seed_org()
    r = await api.post(base(world), body, api.h(world.owner))
    assert r.status_code == 422
    assert [i for i in await rows(api, Invite)] == []


async def test_domain_restricted_needs_a_verified_domain(api):
    world = await api.seed_org()
    r = await api.post(base(world), {"max_uses": 5, "expires_in_days": 7, "domain_restricted": True}, api.h(world.owner))
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR" and "verify a domain first" in r.json()["detail"]
    async with unit_of_work(api.sf) as s:                                  # a domain that is not verified yet does not count
        s.add(OrgDomain(org_id=world.org.id, domain="amity.edu", verify_token="t", verified_at=None, auto_join=False))
    assert (await api.post(base(world), {"max_uses": 5, "expires_in_days": 7, "domain_restricted": True}, api.h(world.owner))).status_code == 422
    await verify_domain(api, world, "other.edu")
    assert (await make_link(api, world, who=world.owner, domain_restricted=True))["domain_restricted"] is True


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_create_status_gate(api, status):
    world = await api.seed_org(status=status)
    r = await api.post(base(world), {"max_uses": 5, "expires_in_days": 7}, api.h(world.owner))
    assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"


async def test_who_may_manage_links(api):
    world, other = await api.seed_org(), await api.seed_org()
    body = {"max_uses": 5, "expires_in_days": 7}
    assert (await api.post(base(world), body, api.h(world.members[0]))).status_code == 403
    assert (await api.post(base(world), body, api.h(other.owner))).status_code == 404
    assert (await api.client.get(base(world), headers=api.h(world.members[0]))).status_code == 403
    assert (await api.client.get(base(world), headers=api.h(other.owner))).status_code == 404
    assert (await api.post(base(world), body, api.h(world.owner))).status_code == 200
    assert (await api.post(base(world), body, api.h(world.admin))).status_code == 200


async def test_list_shows_status_and_never_the_url(api):
    world = await api.seed_org()
    open_ = await make_link(api, world, max_uses=5)
    used = await make_link(api, world, max_uses=1)
    late = await make_link(api, world, max_uses=5)
    gone = await make_link(api, world, max_uses=5)
    async with unit_of_work(api.sf) as s:
        (await s.get(Invite, uuid.UUID(used["link_id"]))).uses = 1
        (await s.get(Invite, uuid.UUID(late["link_id"]))).expires_at = NOW - timedelta(seconds=1)
        row = await s.get(Invite, uuid.UUID(gone["link_id"]))
        row.revoked_at, row.revoke_reason = NOW, "revoked"
    r = await api.client.get(base(world), headers=api.h(world.admin))
    assert r.status_code == 200
    by_id = {i["link_id"]: i for i in r.json()["items"]}
    assert {k: by_id[v["link_id"]]["status"] for k, v in (("open", open_), ("used", used), ("late", late), ("gone", gone))} == {
        "open": "open", "used": "used_up", "late": "expired", "gone": "revoked"}
    assert set(by_id[open_["link_id"]]) == {"link_id", "created_by", "created_at", "expires_at", "max_uses", "uses",
                                           "domain_restricted", "status"}
    assert "url" not in r.text and "/join/" not in r.text and "token" not in r.text
    assert [i["link_id"] for i in r.json()["items"]][0] == gone["link_id"]              # newest first


async def test_the_email_invite_list_does_not_show_links(api):
    world = await api.seed_org()
    await make_link(api, world)
    assert (await api.client.get(f"/org/v1/orgs/{world.org.id}/invites", headers=api.h(world.admin))).json()["items"] == []


async def test_view_the_url_again_is_the_same_url_and_is_audited(api):
    world = await api.seed_org()
    made = await make_link(api, world)
    r = await api.client.get(f"{base(world)}/{made['link_id']}/url", headers=api.h(world.owner))
    assert r.status_code == 200 and r.json() == {"link_id": made["link_id"], "url": made["url"]}
    (audit,) = [a for a in await rows(api, AuditLog) if a.action == "invite_link.url_viewed"]
    assert audit.actor_id == world.owner.id and str(audit.target_id) == made["link_id"] and made["url"] not in str(audit.details)
    assert (await api.client.get(f"{base(world)}/{made['link_id']}/url", headers=api.h(world.members[0]))).status_code == 403


@pytest.mark.parametrize("how", ["revoked", "used_up", "expired"])
async def test_only_open_links_show_their_url(api, how):
    world = await api.seed_org()
    made = await make_link(api, world, max_uses=1)
    async with unit_of_work(api.sf) as s:
        row = await s.get(Invite, uuid.UUID(made["link_id"]))
        if how == "revoked":
            row.revoked_at, row.revoke_reason = NOW, "revoked"
        elif how == "used_up":
            row.uses = 1
        else:
            row.expires_at = NOW - timedelta(seconds=1)
    r = await api.client.get(f"{base(world)}/{made['link_id']}/url", headers=api.h(world.owner))
    assert r.status_code == 404 and r.json()["code"] == "INVITE_INVALID"
    assert [a for a in await rows(api, AuditLog) if a.action == "invite_link.url_viewed"] == []


async def test_a_link_of_another_org_or_an_email_invite_is_404_everywhere(api):
    world, other = await api.seed_org(), await api.seed_org()
    theirs = await make_link(api, other)
    async with unit_of_work(api.sf) as s:
        email_invite = Invite(id=uuid.uuid4(), org_id=world.org.id, kind="email", email="e@amity.edu", role="member",
                              token_hash="e1", expires_at=NOW + timedelta(days=1), created_at=NOW)
        s.add(email_invite)
    for link_id in (theirs["link_id"], str(email_invite.id), str(uuid.uuid4())):
        assert (await api.client.get(f"{base(world)}/{link_id}/url", headers=api.h(world.owner))).status_code == 404
        r = await api.client.delete(f"{base(world)}/{link_id}", headers={**api.h(world.owner), "Idempotency-Key": uuid.uuid4().hex})
        assert r.status_code == 404


async def test_revoke_then_the_link_is_dead_and_a_second_revoke_is_a_quiet_204(api):
    world = await api.seed_org()
    made = await make_link(api, world)
    token = token_of(made["url"])
    assert (await api.client.get(PREVIEW, params={"token": token})).status_code == 200
    delete = lambda: api.client.delete(f"{base(world)}/{made['link_id']}",                    # noqa: E731
                                       headers={**api.h(world.admin), "Idempotency-Key": uuid.uuid4().hex})
    assert (await delete()).status_code == 204
    (link,) = [i for i in await rows(api, Invite) if i.kind == "link"]
    assert (link.revoked_at, link.revoke_reason) == (NOW, "revoked")
    assert (await api.client.get(PREVIEW, params={"token": token})).status_code == 404
    _, headers = await user(api)
    assert (await accept(api, token, headers)).status_code == 404
    api.clock.advance(seconds=60)
    assert (await delete()).status_code == 204
    assert (await rows(api, Invite))[0].revoked_at == NOW                                      # not rewritten
    assert len([a for a in await rows(api, AuditLog) if a.action == "invite.revoked"]) == 1


# ---- accepting
async def test_accept_joins_as_a_member_with_source_link_and_counts_the_use(api):
    world = await api.seed_org(name="Amity University")
    made = await make_link(api, world)
    user_id, headers = await user(api)
    r = await accept(api, token_of(made["url"]), headers)
    assert r.status_code == 200 and r.json() == {"org_id": str(world.org.id), "org_name": "Amity University", "role": "member"}
    (member,) = [m for m in await rows(api, Membership) if m.user_id == user_id]
    assert (member.org_id, member.role, member.source, member.status, member.invited_by) == (
        world.org.id, "member", "link", "active", world.admin.id)
    (link,) = [i for i in await rows(api, Invite) if i.kind == "link"]
    assert (link.uses, link.accepted_at, link.accepted_by, link.revoked_at) == (1, None, None, None)     # a link is never "accepted"
    (event,) = [e for e in await rows(api, OutboxEvent) if e.topic == "org.member.joined"]
    assert event.payload["source"] == "link" and event.payload["user_id"] == str(user_id) and event.payload["org_type"] == "college"
    joined = [a for a in await rows(api, AuditLog) if a.action == "member.joined"]
    assert joined[-1].details["source"] == "link" and joined[-1].details["invite_id"] == made["link_id"]
    assert (await api.client.get("/org/v1/me", headers=headers)).json()["role"] == "member"


async def test_the_link_is_not_bound_to_an_email(api):
    world = await api.seed_org()
    token = token_of((await make_link(api, world))["url"])
    for email in ("someone@gmail.com", "other@yahoo.in"):
        _, headers = await user(api, email)
        assert (await accept(api, token, headers)).status_code == 200


async def test_the_same_user_retrying_gets_200_and_one_membership(api):
    world = await api.seed_org()
    made = await make_link(api, world, max_uses=1)
    token = token_of(made["url"])
    user_id, headers = await user(api)
    first, second = await accept(api, token, headers), await accept(api, token, headers)     # a real retry: a new key
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len([m for m in await rows(api, Membership) if m.user_id == user_id]) == 1
    assert [i for i in await rows(api, Invite) if i.kind == "link"][0].uses == 1               # the retry did not count again
    _, other = await user(api)
    assert (await accept(api, token, other)).status_code == 404                                # but a different user finds it used up


async def test_used_up_is_404_for_accept_and_preview(api):
    world = await api.seed_org()
    token = token_of((await make_link(api, world, max_uses=2))["url"])
    for _ in range(2):
        assert (await accept(api, token, (await user(api))[1])).status_code == 200
    r = await accept(api, token, (await user(api))[1])
    assert r.status_code == 404 and r.json()["code"] == "INVITE_INVALID"
    p = await api.client.get(PREVIEW, params={"token": token})
    assert p.status_code == 404 and p.json()["code"] == "INVITE_INVALID"


async def test_expired_is_410_and_the_link_gets_closed(api):
    world = await api.seed_org()
    made = await make_link(api, world, days=1)
    api.clock.advance(seconds=86400 + 1)
    token = token_of(made["url"])
    r = await accept(api, token, (await user(api))[1])
    assert r.status_code == 410 and r.json()["code"] == "INVITE_EXPIRED"
    (link,) = [i for i in await rows(api, Invite) if i.kind == "link"]
    assert link.revoke_reason == "expired" and link.uses == 0
    assert (await api.client.get(PREVIEW, params={"token": token})).status_code == 410


async def test_max_uses_holds_under_10_concurrent_accepts(api):
    world = await api.seed_org(seats=100)
    token = token_of((await make_link(api, world, max_uses=5))["url"])
    people = [await user(api) for _ in range(10)]
    results = await asyncio.gather(*[accept(api, token, headers) for _, headers in people])
    codes = sorted(r.status_code for r in results)
    assert codes == [200] * 5 + [404] * 5, codes
    joined = {m.user_id for m in await rows(api, Membership) if m.source == "link"}
    assert len(joined) == 5 and joined == {uid for (uid, _), r in zip(people, results) if r.status_code == 200}
    assert [i for i in await rows(api, Invite) if i.kind == "link"][0].uses == 5
    assert len([e for e in await rows(api, OutboxEvent) if e.topic == "org.member.joined"]) == 5


async def test_a_full_org_refuses_and_the_link_keeps_its_uses(api):
    world = await api.seed_org(seats=5, admins=1, members=3)                                  # 5 of 5
    made = await make_link(api, world)
    r = await accept(api, token_of(made["url"]), (await user(api))[1])
    assert r.status_code == 409 and r.json()["code"] == "SEATS_FULL"
    assert [i for i in await rows(api, Invite) if i.kind == "link"][0].uses == 0


async def test_someone_already_in_an_org_gets_409(api):
    world, other = await api.seed_org(), await api.seed_org()
    token = token_of((await make_link(api, world))["url"])
    r = await accept(api, token, api.h(other.member if hasattr(other, "member") else other.members[0]))
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_IN_ORG"
    assert [i for i in await rows(api, Invite) if i.kind == "link"][0].uses == 0


# ---- domain restriction
async def test_a_domain_restricted_link_admits_only_a_verified_domain(api):
    world = await api.seed_org()
    await verify_domain(api, world, "amity.edu")
    token = token_of((await make_link(api, world, who=world.owner, domain_restricted=True))["url"])
    outsider_id, outsider = await user(api, "someone@gmail.com")
    r = await accept(api, token, outsider)
    assert r.status_code == 403 and r.json()["code"] == "INVITE_EMAIL_MISMATCH"
    assert [m for m in await rows(api, Membership) if m.user_id == outsider_id] == []
    assert [i for i in await rows(api, Invite) if i.kind == "link"][0].uses == 0
    lookalike = await accept(api, token, (await user(api, "x@notamity.edu"))[1])
    assert lookalike.status_code == 403
    sub = await accept(api, token, (await user(api, "x@mail.amity.edu"))[1])
    assert sub.status_code == 403                                                              # a subdomain is a different domain
    member_id, member = await user(api, "Student@AMITY.edu")
    assert (await accept(api, token, member)).status_code == 200
    assert [m for m in await rows(api, Membership) if m.user_id == member_id][0].source == "link"


async def test_an_unrestricted_link_ignores_domains(api):
    world = await api.seed_org()
    await verify_domain(api, world, "amity.edu")
    token = token_of((await make_link(api, world))["url"])
    assert (await accept(api, token, (await user(api, "someone@gmail.com"))[1])).status_code == 200


# ---- preview
async def test_preview_of_a_link(api):
    world = await api.seed_org(name="Amity University", type="college")
    made = await make_link(api, world, who=world.owner, days=3)
    r = await api.client.get(PREVIEW, params={"token": token_of(made["url"])})
    assert r.status_code == 200
    assert r.json() == {"org_name": "Amity University", "org_type": "college", "inviter_name": world.owner.name, "role": "member",
                        "email_hint": None, "expires_at": r.json()["expires_at"]}
    assert r.json()["expires_at"].startswith((NOW + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"))


async def test_preview_and_accept_of_a_link_in_an_inactive_org_are_refused(api):
    world = await api.seed_org()
    token = token_of((await make_link(api, world))["url"])
    async with unit_of_work(api.sf) as s:
        from app.repositories.org_repository import OrgRepository
        (await OrgRepository(s).get(world.org.id)).status = "suspended"
    assert (await api.client.get(PREVIEW, params={"token": token})).status_code == 404
    r = await accept(api, token, (await user(api))[1])
    assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"


async def test_a_domain_restricted_link_uses_the_matching_rules_for_subdomains(api):
    world = await api.seed_org()
    await verify_domain(api, world, "amity.edu")
    token = token_of((await make_link(api, world, who=world.owner, domain_restricted=True))["url"])
    assert (await accept(api, token, (await user(api, "x@cse.amity.edu"))[1])).status_code == 403          # exact domain only
    async with unit_of_work(api.sf) as s:
        (await s.get(OrgDomain, (world.org.id, "amity.edu"))).include_subdomains = True
    deep = await accept(api, token, (await user(api, "y@cse.student.amity.edu"))[1])
    assert deep.status_code == 200                                                                          # any depth once included
    assert (await accept(api, token, (await user(api, "z@notamity.edu"))[1])).status_code == 403            # still not a look-alike


async def test_a_restricted_link_refuses_an_email_that_belongs_to_another_orgs_domain(api):
    world, other = await api.seed_org(), await api.seed_org()
    await verify_domain(api, world, "amity.edu")
    await verify_domain(api, other, "rival.edu")
    token = token_of((await make_link(api, world, who=world.owner, domain_restricted=True))["url"])
    r = await accept(api, token, (await user(api, "x@rival.edu"))[1])
    assert r.status_code == 403 and r.json()["code"] == "INVITE_EMAIL_MISMATCH"


async def test_the_longest_match_decides_which_org_a_restricted_link_serves(api):
    parent, child = await api.seed_org(), await api.seed_org()
    async with unit_of_work(api.sf) as s:
        s.add(OrgDomain(org_id=parent.org.id, domain="amity.edu", verify_token="t" * 43, verified_at=NOW, verify_method="dns",
                        auto_join=False, include_subdomains=True, failed_checks=0, added_at=NOW))
        s.add(OrgDomain(org_id=child.org.id, domain="cse.amity.edu", verify_token="u" * 43, verified_at=NOW, verify_method="dns",
                        auto_join=False, include_subdomains=False, failed_checks=0, added_at=NOW))
    parent_link = token_of((await make_link(api, parent, who=parent.owner, domain_restricted=True))["url"])
    child_link = token_of((await make_link(api, child, who=child.owner, domain_restricted=True))["url"])
    _, student = await user(api, "a@cse.amity.edu")
    assert (await accept(api, parent_link, student)).status_code == 403                                      # cse.amity.edu is the child's
    assert (await accept(api, child_link, student)).status_code == 200
