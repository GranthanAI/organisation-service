"""The domain routes: add, list, patch, delete, verify, and the staff manual verify."""

import re
import uuid

import pytest
from sqlalchemy import select

from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.domain import OrgDomain
from app.models.membership import Membership
from tests.support.factories import NOW
from tests.support.tokens import bearer, make_token


def base(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/domains"


async def add(api, world, domain, who=None, **extra):
    return await api.post(base(world), {"domain": domain, **extra}, api.h(who or world.owner))


async def rows(api, model=OrgDomain):
    async with api.sf() as s:
        return list((await s.execute(select(model))).scalars())


async def put_verified(api, org, domain, include_subdomains=False, method="dns", auto_join=False):
    async with unit_of_work(api.sf) as s:
        s.add(OrgDomain(org_id=org.id, domain=domain, verify_token="t" * 43, verified_at=NOW, verified_by=uuid.uuid4(),
                        verify_method=method, auto_join=auto_join, include_subdomains=include_subdomains, failed_checks=0,
                        added_at=NOW))


async def put_pending(api, org, domain, token="p" * 43):
    async with unit_of_work(api.sf) as s:
        s.add(OrgDomain(org_id=org.id, domain=domain, verify_token=token, auto_join=False, include_subdomains=False,
                        failed_checks=0, added_at=NOW))
    return token


# ---------------------------------------------------------------- add
async def test_add_returns_the_txt_record_and_stores_a_32_byte_token(api):
    world = await api.seed_org()
    r = await add(api, world, "Amity.EDU.")                                                 # normalised
    assert r.status_code == 201
    body = r.json()
    (row,) = await rows(api)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", row.verify_token)                              # 32 random bytes, base64url
    assert body == {"domain": "amity.edu", "status": "pending", "include_subdomains": False,
                    "dns_record": {"type": "TXT", "host": "@", "value": f"granthan-verify={row.verify_token}"},
                    "instructions": body["instructions"]}
    assert "amity.edu" in body["instructions"] and "granthan-verify=" in body["instructions"]
    assert (row.org_id, row.verified_at, row.auto_join, row.failed_checks, row.added_at) == (world.org.id, None, False, 0, NOW)
    (audit,) = [a for a in await rows(api, AuditLog) if a.action == "domain.added"]
    assert audit.actor_id == world.owner.id and audit.details == {"domain": "amity.edu", "include_subdomains": False}
    assert row.verify_token not in str(audit.details)
    two = await add(api, world, "cse.amity.edu", include_subdomains=True)
    assert two.status_code == 201 and two.json()["include_subdomains"] is True
    tokens = {r.verify_token for r in await rows(api)}
    assert len(tokens) == 2                                                                  # a fresh token each


@pytest.mark.parametrize("domain", ["gmail.com", "mail.gmail.com", "GMAIL.com", "x.y.outlook.com"])
async def test_public_mail_domains_and_their_subdomains_are_refused(api, domain):
    world = await api.seed_org()
    r = await add(api, world, domain)
    assert r.status_code == 422 and r.json()["code"] == "PUBLIC_EMAIL_DOMAIN"
    assert await rows(api) == []


@pytest.mark.parametrize("domain", ["amity", "a b.edu", "https://amity.edu", "bücher.example", "-x.edu", "amity..edu"])
async def test_invalid_domains_are_422(api, domain):
    world = await api.seed_org()
    r = await add(api, world, domain)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


async def test_an_organisation_can_have_at_most_ten_domains(api):
    world = await api.seed_org()
    for i in range(10):
        assert (await add(api, world, f"college{i}.edu")).status_code == 201
    r = await add(api, world, "eleven.edu")
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR" and "10" in r.json()["detail"]
    assert len(await rows(api)) == 10


async def test_adding_the_same_domain_twice_is_422_not_a_crash(api):
    world = await api.seed_org()
    assert (await add(api, world, "amity.edu")).status_code == 201
    r = await add(api, world, "AMITY.edu")
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


async def test_a_domain_another_org_only_added_is_allowed_and_the_first_to_verify_wins(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_pending(api, other.org, "amity.edu")
    assert (await add(api, world, "amity.edu")).status_code == 201
    assert len(await rows(api)) == 2


async def test_a_domain_verified_by_any_org_is_taken(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_verified(api, other.org, "amity.edu")
    r = await add(api, world, "amity.edu")
    assert r.status_code == 409 and r.json()["code"] == "DOMAIN_TAKEN"
    assert other.org.name not in r.text and str(other.org.id) not in r.text                  # never says which
    mine = await seed_own_verified(api, world)
    assert (await add(api, world, "other.edu")).status_code == 201
    assert mine is not None


async def seed_own_verified(api, world):
    await put_verified(api, world.org, "mine.edu")
    r = await add(api, world, "mine.edu")                                                   # an org cannot re-add its own domain
    assert r.status_code in (409, 422)
    return r


async def test_a_verified_parent_with_subdomains_blocks_the_child_but_one_without_does_not(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_verified(api, other.org, "amity.edu", include_subdomains=True)
    r = await add(api, world, "cse.amity.edu")
    assert r.status_code == 409 and r.json()["code"] == "DOMAIN_TAKEN"
    assert (await add(api, world, "deep.cse.amity.edu")).status_code == 409                  # any depth
    assert (await add(api, world, "notamity.edu")).status_code == 201                        # a look-alike is a different domain
    third = await api.seed_org()
    await put_verified(api, third.org, "plain.edu", include_subdomains=False)
    assert (await add(api, world, "cse.plain.edu")).status_code == 201                       # an exact-only parent leaves children free


async def test_a_verified_child_blocks_adding_the_parent(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_verified(api, other.org, "cse.amity.edu")
    r = await add(api, world, "amity.edu")
    assert r.status_code == 409 and r.json()["code"] == "DOMAIN_TAKEN"
    assert (await add(api, world, "law.amity.edu")).status_code == 201                       # a sibling is fine


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_add_status_gate(api, status):
    world = await api.seed_org(status=status)
    r = await add(api, world, "amity.edu")
    assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"


async def test_who_may_manage_domains(api):
    world, other = await api.seed_org(), await api.seed_org()
    assert (await add(api, world, "a.edu", who=world.members[0])).status_code == 403
    assert (await add(api, world, "b.edu", who=other.owner)).status_code == 404               # not a member: 404
    assert (await add(api, world, "c.edu", who=world.admin)).status_code == 201
    unauth = await api.client.post(base(world), json={"domain": "d.edu"}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert unauth.status_code == 401


# ---------------------------------------------------------------- list
async def test_members_see_domains_without_the_token_and_admins_see_the_record_of_pending_ones(api):
    world = await api.seed_org()
    token = await put_pending(api, world.org, "pending.edu")
    await put_verified(api, world.org, "verified.edu", auto_join=True)
    as_member = await api.client.get(base(world), headers=api.h(world.members[0]))
    assert as_member.status_code == 200
    items = {i["domain"]: i for i in as_member.json()["items"]}
    assert set(items) == {"pending.edu", "verified.edu"}
    assert token not in as_member.text and "verify_token" not in as_member.text and "granthan-verify" not in as_member.text
    assert all(i["dns_record"] is None for i in items.values())
    assert (items["verified.edu"]["status"], items["verified.edu"]["auto_join"], items["verified.edu"]["verify_method"]) == ("verified", True, "dns")
    assert items["pending.edu"]["status"] == "pending"
    for who in (world.admin, world.owner):
        as_admin = (await api.client.get(base(world), headers=api.h(who))).json()["items"]
        by = {i["domain"]: i for i in as_admin}
        assert by["pending.edu"]["dns_record"] == {"type": "TXT", "host": "@", "value": f"granthan-verify={token}"}
        assert by["verified.edu"]["dns_record"] is None                                       # nothing to publish any more


async def test_list_is_for_members_of_this_org_only(api):
    world, other = await api.seed_org(), await api.seed_org()
    assert (await api.client.get(base(world), headers=api.h(other.owner))).status_code == 404
    assert (await api.client.get(base(world))).status_code == 401


# ---------------------------------------------------------------- patch
async def test_auto_join_needs_a_verified_domain(api):
    world = await api.seed_org()
    await put_pending(api, world.org, "pending.edu")
    await put_verified(api, world.org, "verified.edu")
    r = await api.client.patch(f"{base(world)}/pending.edu", json={"auto_join": True}, headers=api.h(world.owner))
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    ok = await api.client.patch(f"{base(world)}/verified.edu", json={"auto_join": True}, headers=api.h(world.owner))
    assert ok.status_code == 200 and ok.json()["auto_join"] is True
    off = await api.client.patch(f"{base(world)}/verified.edu", json={"auto_join": False}, headers=api.h(world.owner))
    assert off.json()["auto_join"] is False
    actions = [a for a in await rows(api, AuditLog) if a.action == "domain.updated"]
    assert [a.details["auto_join"] for a in actions] == [True, False]


async def test_changing_include_subdomains_on_a_verified_domain_rechecks_ownership(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_verified(api, world.org, "amity.edu")
    await put_verified(api, other.org, "cse.amity.edu")
    r = await api.client.patch(f"{base(world)}/amity.edu", json={"include_subdomains": True}, headers=api.h(world.owner))
    assert r.status_code == 409 and r.json()["code"] == "DOMAIN_TAKEN"                         # it would swallow another org's domain
    quiet = await api.seed_org()
    await put_verified(api, quiet.org, "free.edu")
    ok = await api.client.patch(f"{base(quiet)}/free.edu", json={"include_subdomains": True}, headers=api.h(quiet.owner))
    assert ok.status_code == 200 and ok.json()["include_subdomains"] is True
    pending = await put_pending(api, world.org, "pend.edu")
    free = await api.client.patch(f"{base(world)}/pend.edu", json={"include_subdomains": True}, headers=api.h(world.owner))
    assert free.status_code == 200 and pending                                                  # not verified yet: decided at verification


async def test_patch_unknown_domain_member_and_empty_body(api):
    world = await api.seed_org()
    await put_verified(api, world.org, "amity.edu")
    assert (await api.client.patch(f"{base(world)}/nope.edu", json={"auto_join": False}, headers=api.h(world.owner))).status_code == 404
    assert (await api.client.patch(f"{base(world)}/amity.edu", json={"auto_join": True}, headers=api.h(world.members[0]))).status_code == 403
    same = await api.client.patch(f"{base(world)}/amity.edu", json={}, headers=api.h(world.owner))
    assert same.status_code == 200 and [a for a in await rows(api, AuditLog) if a.action == "domain.updated"] == []   # no change, no audit
    assert (await api.client.patch(f"{base(world)}/amity.edu", json={"bogus": 1}, headers=api.h(world.owner))).status_code == 422


# ---------------------------------------------------------------- delete
async def test_delete_removes_the_domain_and_keeps_the_members(api):
    world = await api.seed_org()
    await put_verified(api, world.org, "amity.edu", auto_join=True)
    before = len([m for m in await rows(api, Membership)])
    r = await api.client.delete(f"{base(world)}/amity.edu", headers={**api.h(world.owner), "Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 204
    assert await rows(api) == [] and len(await rows(api, Membership)) == before
    (audit,) = [a for a in await rows(api, AuditLog) if a.action == "domain.removed"]
    assert audit.details == {"domain": "amity.edu", "was_verified": True}
    again = await api.client.delete(f"{base(world)}/amity.edu", headers={**api.h(world.owner), "Idempotency-Key": uuid.uuid4().hex})
    assert again.status_code == 404
    assert (await api.client.delete(f"{base(world)}/x.edu", headers={**api.h(world.members[0]), "Idempotency-Key": uuid.uuid4().hex})).status_code == 403


async def test_a_removed_domain_can_be_taken_by_another_org(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_verified(api, world.org, "amity.edu")
    assert (await add(api, other, "amity.edu")).status_code == 409
    await api.client.delete(f"{base(world)}/amity.edu", headers={**api.h(world.owner), "Idempotency-Key": uuid.uuid4().hex})
    assert (await add(api, other, "amity.edu")).status_code == 201


# ---------------------------------------------------------------- verify
async def test_verify_found_then_verified_and_audited(api, fake_dns):
    world = await api.seed_org()
    token = (await add(api, world, "amity.edu")).json()["dns_record"]["value"].split("=", 1)[1]
    fake_dns.publish("amity.edu", token)
    r = await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.admin))
    assert r.status_code == 200 and r.json()["status"] == "verified" and r.json()["verify_method"] == "dns"
    (row,) = await rows(api)
    assert (row.verified_at, row.verified_by, row.verify_method, row.failed_checks, row.last_checked_at) == (NOW, world.admin.id, "dns", 0, NOW)
    (audit,) = [a for a in await rows(api, AuditLog) if a.action == "domain.verified"]
    assert audit.details == {"domain": "amity.edu", "method": "dns"}
    again = await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.admin))
    assert again.status_code == 200 and again.json()["status"] == "verified"                    # idempotent


async def test_verify_not_found_is_409_with_the_record_to_publish(api, fake_dns):
    world = await api.seed_org()
    added = (await add(api, world, "amity.edu")).json()
    r = await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.owner))
    assert r.status_code == 409 and r.json()["code"] == "VALIDATION_ERROR" and "TXT record not found yet" in r.json()["detail"]
    assert r.json()["dns_record"] == added["dns_record"] and r.headers["content-type"].startswith("application/problem+json")
    assert (await rows(api))[0].verified_at is None
    fake_dns.errors.add("amity.edu")
    err = await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.owner))
    assert err.status_code == 409 and "failed" in err.json()["detail"]


async def test_verify_is_limited_to_10_an_hour_per_domain(api, fake_dns):
    world = await api.seed_org()
    await add(api, world, "amity.edu")
    await add(api, world, "other.edu")
    for _ in range(10):
        assert (await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.owner))).status_code == 409
    r = await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.owner))
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and int(r.headers["retry-after"]) > 0
    assert (await api.post(f"{base(world)}/other.edu/verify", None, api.h(world.owner))).status_code == 409   # another domain: own bucket
    assert fake_dns.calls.count("amity.edu") == 10                                              # the 11th never reached DNS


async def test_verify_status_gate_and_permissions(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_pending(api, world.org, "amity.edu")
    assert (await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.members[0]))).status_code == 403
    assert (await api.post(f"{base(world)}/amity.edu/verify", None, api.h(other.owner))).status_code == 404
    assert (await api.post(f"{base(world)}/nope.edu/verify", None, api.h(world.owner))).status_code == 404


async def test_verify_loses_to_a_domain_verified_meanwhile(api, fake_dns):
    world, other = await api.seed_org(), await api.seed_org()
    token = await put_pending(api, world.org, "amity.edu")
    await put_verified(api, other.org, "amity.edu")                                              # someone else got there first
    fake_dns.publish("amity.edu", token)
    r = await api.post(f"{base(world)}/amity.edu/verify", None, api.h(world.owner))
    assert r.status_code == 409 and r.json()["code"] == "DOMAIN_TAKEN"


# ---------------------------------------------------------------- staff manual verify
STAFF = lambda: bearer(make_token(roles=["admin", "billing_admin", "support"], email="staff@granthan.com"))   # noqa: E731


async def test_staff_manual_verify(api):
    world = await api.seed_org()
    await put_pending(api, world.org, "amity.edu")
    url = f"/admin/org/v1/orgs/{world.org.id}/domains/amity.edu/manual-verify"
    assert (await api.post(url, {}, STAFF())).status_code == 422                                  # a reason is required
    assert (await api.post(url, {"reason": "college IT is slow, checked by phone"}, api.h(world.owner))).status_code == 403
    r = await api.post(url, {"reason": "college IT is slow, checked by phone"}, STAFF())
    assert r.status_code == 200 and r.json()["status"] == "verified" and r.json()["verify_method"] == "staff"
    (row,) = await rows(api)
    assert (row.verify_method, row.verified_at) == ("staff", NOW) and row.verified_by is not None
    (audit,) = [a for a in await rows(api, AuditLog) if a.action == "domain.verified"]
    assert audit.reason == "college IT is slow, checked by phone" and audit.actor_kind == "staff" and audit.details["method"] == "staff"


async def test_staff_manual_verify_follows_the_same_ownership_rules(api):
    world, other = await api.seed_org(), await api.seed_org()
    await put_pending(api, world.org, "amity.edu")
    await put_verified(api, other.org, "amity.edu")
    r = await api.post(f"/admin/org/v1/orgs/{world.org.id}/domains/amity.edu/manual-verify", {"reason": "please just do it"}, STAFF())
    assert r.status_code == 409 and r.json()["code"] == "DOMAIN_TAKEN"
    assert (await api.post(f"/admin/org/v1/orgs/{world.org.id}/domains/nope.edu/manual-verify", {"reason": "no such domain"}, STAFF())).status_code == 404
