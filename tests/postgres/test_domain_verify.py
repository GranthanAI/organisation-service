"""Domain verification: the DNS check, the race between orgs, the hourly and daily jobs, staff verification, the rate limit."""

import asyncio
import uuid
from datetime import timedelta

import fakeredis
import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.constants import RATE_DOMAIN_VERIFY
from app.core.enums import Role
from app.core.exceptions import DomainTaken, RateLimited
from app.db.session import unit_of_work
from app.middleware.rate_limit import hit_fail_open
from app.models.audit_log import AuditLog
from app.models.domain import OrgDomain
from app.repositories.org_repository import OrgRepository
from app.security.models import CurrentUser, OrgContext
from app.services import domain_jobs, domain_service
from app.services.permission_rules import Action
from app.telemetry.metrics import DOMAIN_CHECKS
from tests.support.factories import NOW, make_org


@pytest.fixture
def clock():
    return FixedClock(NOW)


def checks(result: str) -> float:
    return DOMAIN_CHECKS.labels(result=result)._value.get()


async def make_org_ctx(session_factory, name="Org"):
    org = make_org(name=name)
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
    user = CurrentUser(id=uuid.uuid4(), email="owner@amity.edu", jti="j")
    return org, OrgContext(org_id=org.id, user=user, role=Role.OWNER, membership_id=uuid.uuid4(), action=Action.MANAGE_DOMAINS)


async def add_row(session_factory, org, domain, *, token=None, added_at=NOW, verified=None, method=None, last_checked_at=None,
                  failed=0, auto_join=False, include_subdomains=False):
    token = token or uuid.uuid4().hex + uuid.uuid4().hex[:11]
    async with unit_of_work(session_factory) as s:
        s.add(OrgDomain(org_id=org.id, domain=domain, verify_token=token, verified_at=verified, verified_by=None,
                        verify_method=method, auto_join=auto_join, include_subdomains=include_subdomains, failed_checks=failed,
                        last_checked_at=last_checked_at, added_at=added_at))
    return token


async def get_row(session_factory, org, domain) -> OrgDomain:
    async with session_factory() as s:
        return await s.get(OrgDomain, (org.id, domain))


async def run(session_factory, handler, clock):
    async with unit_of_work(session_factory) as s:
        await handler(s, None, clock)


async def audits(session_factory, action):
    async with session_factory() as s:
        return [a for a in (await s.execute(select(AuditLog))).scalars() if a.action == action]


# ---------------------------------------------------------------- the check
async def test_a_txt_record_found_verifies(session_factory, clock, fake_dns):
    org, ctx = await make_org_ctx(session_factory)
    token = await add_row(session_factory, org, "amity.edu")
    fake_dns.publish("amity.edu", token, extra=("v=spf1 include:_spf.google.com ~all",))       # next to an unrelated TXT record
    found_before = checks("found")
    async with unit_of_work(session_factory) as s:
        outcome = await domain_service.verify_now(s, ctx, "amity.edu", clock)
    assert outcome.verified and checks("found") == found_before + 1
    row = await get_row(session_factory, org, "amity.edu")
    assert (row.verified_at, row.verify_method, row.verified_by, row.failed_checks) == (NOW, "dns", ctx.user.id, 0)


async def test_a_txt_value_split_across_strings_still_matches(session_factory, clock, fake_dns):
    org, ctx = await make_org_ctx(session_factory)
    token = await add_row(session_factory, org, "amity.edu")
    fake_dns.publish("amity.edu", token, split=True)
    async with unit_of_work(session_factory) as s:
        assert (await domain_service.verify_now(s, ctx, "amity.edu", clock)).verified
    assert [len(p) for p in fake_dns.records["amity.edu"][-1]][0] == 10                          # it really was split


async def test_not_found_changes_nothing_and_returns_the_record(session_factory, clock, fake_dns):
    org, ctx = await make_org_ctx(session_factory)
    token = await add_row(session_factory, org, "amity.edu")
    fake_dns.records["amity.edu"] = [["granthan-verify=wrong"], ["something else"]]
    before = checks("not_found")
    async with unit_of_work(session_factory) as s:
        outcome = await domain_service.verify_now(s, ctx, "amity.edu", clock)
    assert not outcome.verified and not outcome.lookup_failed and checks("not_found") == before + 1
    assert outcome.record.value == f"granthan-verify={token}" and (outcome.record.type, outcome.record.host) == ("TXT", "@")
    assert (await get_row(session_factory, org, "amity.edu")).verified_at is None
    assert await audits(session_factory, "domain.verified") == []


async def test_a_dns_timeout_is_not_found_and_counted_as_an_error(session_factory, clock, fake_dns):
    org, ctx = await make_org_ctx(session_factory)
    await add_row(session_factory, org, "amity.edu")
    fake_dns.errors.add("amity.edu")
    before = checks("error")
    async with unit_of_work(session_factory) as s:
        outcome = await domain_service.verify_now(s, ctx, "amity.edu", clock)
    assert not outcome.verified and outcome.lookup_failed and checks("error") == before + 1
    assert (await get_row(session_factory, org, "amity.edu")).verified_at is None


async def test_two_orgs_racing_for_one_domain_give_one_winner_and_domain_taken(session_factory, clock, fake_dns):
    (a, ctx_a), (b, ctx_b) = await make_org_ctx(session_factory, "A"), await make_org_ctx(session_factory, "B")
    token_a, token_b = await add_row(session_factory, a, "amity.edu"), await add_row(session_factory, b, "amity.edu")
    fake_dns.records["amity.edu"] = [[f"granthan-verify={token_a}"], [f"granthan-verify={token_b}"]]    # both published (a mistake, or a prank)

    async def verify(ctx):
        async with unit_of_work(session_factory) as s:
            return await domain_service.verify_now(s, ctx, "amity.edu", clock)

    results = await asyncio.gather(verify(ctx_a), verify(ctx_b), return_exceptions=True)
    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, DomainTaken)]
    assert len(winners) == 1 and len(losers) == 1 and winners[0].verified
    verified = [r for r in (await get_row(session_factory, a, "amity.edu"), await get_row(session_factory, b, "amity.edu")) if r.verified_at]
    assert len(verified) == 1
    assert len(await audits(session_factory, "domain.verified")) == 1                              # the loser's audit rolled back


# ---------------------------------------------------------------- domains.verify (hourly)
async def test_the_hourly_job_verifies_a_pending_domain_when_the_record_appears(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    token = await add_row(session_factory, org, "amity.edu")
    await run(session_factory, domain_jobs.domains_verify, clock)
    row = await get_row(session_factory, org, "amity.edu")
    assert row.verified_at is None and row.last_checked_at == NOW                                 # looked, not there yet
    fake_dns.publish("amity.edu", token)
    clock.advance(seconds=3600)
    await run(session_factory, domain_jobs.domains_verify, clock)
    row = await get_row(session_factory, org, "amity.edu")
    assert (row.verified_at, row.verify_method, row.verified_by) == (clock.now(), "dns", None)
    (audit,) = await audits(session_factory, "domain.verified")
    assert audit.actor_kind == "system" and audit.details == {"domain": "amity.edu", "method": "dns"}


async def test_the_hourly_job_stops_checking_after_seven_days(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    old = await add_row(session_factory, org, "old.edu", added_at=NOW - timedelta(days=7, seconds=1))
    young = await add_row(session_factory, org, "young.edu", added_at=NOW - timedelta(days=6, hours=23))
    fake_dns.publish("old.edu", old)
    fake_dns.publish("young.edu", young)
    await run(session_factory, domain_jobs.domains_verify, clock)
    assert fake_dns.calls == ["young.edu"]                                                        # the old one is not even looked up
    assert (await get_row(session_factory, org, "young.edu")).verified_at is not None
    assert (await get_row(session_factory, org, "old.edu")).verified_at is None                   # stays pending, manual verify still works
    ctx = OrgContext(org_id=org.id, user=CurrentUser(id=uuid.uuid4(), email="o@amity.edu", jti="j"), role=Role.OWNER,
                     membership_id=uuid.uuid4(), action=Action.MANAGE_DOMAINS)
    async with unit_of_work(session_factory) as s:
        assert (await domain_service.verify_now(s, ctx, "old.edu", clock)).verified


async def test_the_hourly_job_skips_a_domain_someone_else_verified(session_factory, clock, fake_dns):
    (a, _), (b, _) = await make_org_ctx(session_factory, "A"), await make_org_ctx(session_factory, "B")
    await add_row(session_factory, a, "amity.edu", verified=NOW, method="dns")
    token_b = await add_row(session_factory, b, "amity.edu")
    fake_dns.publish("amity.edu", token_b)
    await run(session_factory, domain_jobs.domains_verify, clock)                                  # no crash, no second owner
    assert (await get_row(session_factory, b, "amity.edu")).verified_at is None


# ---------------------------------------------------------------- domains.recheck (daily)
async def test_recheck_found_resets_the_failures(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    token = await add_row(session_factory, org, "amity.edu", verified=NOW - timedelta(days=40), method="dns",
                          last_checked_at=NOW - timedelta(days=31), failed=2, auto_join=True)
    fake_dns.publish("amity.edu", token)
    await run(session_factory, domain_jobs.domains_recheck, clock)
    row = await get_row(session_factory, org, "amity.edu")
    assert (row.failed_checks, row.last_checked_at, row.auto_join) == (0, NOW, True) and row.verified_at is not None


async def test_three_failed_rechecks_remove_the_verification_and_auto_join(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    await add_row(session_factory, org, "amity.edu", verified=NOW - timedelta(days=40), method="dns",
                  last_checked_at=NOW - timedelta(days=31), auto_join=True)
    for expected in (1, 2):
        await run(session_factory, domain_jobs.domains_recheck, clock)
        row = await get_row(session_factory, org, "amity.edu")
        assert (row.failed_checks, row.verified_at is not None, row.auto_join) == (expected, True, True)
        clock.advance(seconds=int(timedelta(days=31).total_seconds()))                            # the 30-day schedule
    await run(session_factory, domain_jobs.domains_recheck, clock)                                # the third failure
    row = await get_row(session_factory, org, "amity.edu")
    assert (row.verified_at, row.verify_method, row.verified_by, row.auto_join, row.failed_checks) == (None, None, None, False, 0)
    (audit,) = await audits(session_factory, "domain.unverified")
    assert audit.actor_kind == "system" and audit.details["domain"] == "amity.edu"


async def test_a_recheck_that_times_out_counts_like_not_found(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    await add_row(session_factory, org, "amity.edu", verified=NOW - timedelta(days=40), method="dns", last_checked_at=NOW - timedelta(days=31))
    fake_dns.errors.add("amity.edu")
    await run(session_factory, domain_jobs.domains_recheck, clock)
    assert (await get_row(session_factory, org, "amity.edu")).failed_checks == 1


async def test_recheck_only_looks_at_dns_verified_domains_that_are_due(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    await add_row(session_factory, org, "staff.edu", verified=NOW - timedelta(days=90), method="staff", last_checked_at=None)
    await add_row(session_factory, org, "fresh.edu", verified=NOW, method="dns", last_checked_at=NOW - timedelta(days=10))
    await add_row(session_factory, org, "pending.edu")
    await add_row(session_factory, org, "never.edu", verified=NOW - timedelta(days=40), method="dns", last_checked_at=None)
    await run(session_factory, domain_jobs.domains_recheck, clock)
    assert fake_dns.calls == ["never.edu"]                                                         # staff-verified: never; fresh: not due


async def test_a_staff_verified_domain_is_never_rechecked_however_old(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    await add_row(session_factory, org, "staff.edu", verified=NOW - timedelta(days=400), method="staff", last_checked_at=None, auto_join=True)
    for _ in range(5):
        clock.advance(seconds=int(timedelta(days=31).total_seconds()))
        await run(session_factory, domain_jobs.domains_recheck, clock)
    row = await get_row(session_factory, org, "staff.edu")
    assert fake_dns.calls == [] and row.verified_at is not None and row.auto_join is True and row.failed_checks == 0


async def test_staff_manual_verify_sets_the_method_staff(session_factory, clock, fake_dns):
    org, _ = await make_org_ctx(session_factory)
    await add_row(session_factory, org, "amity.edu")
    staff = CurrentUser(id=uuid.uuid4(), email="staff@granthan.com", jti="j", roles=["admin"])
    async with unit_of_work(session_factory) as s:
        await domain_service.manual_verify(s, staff, org.id, "amity.edu", "verified over the phone with the registrar", clock)
    row = await get_row(session_factory, org, "amity.edu")
    assert (row.verify_method, row.verified_by, row.verified_at) == ("staff", staff.id, NOW) and fake_dns.calls == []


# ---------------------------------------------------------------- the manual-verify rate limit
async def test_the_verify_limit_is_10_an_hour_per_domain_and_fails_open(clock):
    redis = fakeredis.FakeAsyncRedis()
    for _ in range(10):
        await hit_fail_open(redis, clock, RATE_DOMAIN_VERIFY, "org1:amity.edu")
    with pytest.raises(RateLimited) as caught:
        await hit_fail_open(redis, clock, RATE_DOMAIN_VERIFY, "org1:amity.edu")
    assert caught.value.retry_after > 0
    await hit_fail_open(redis, clock, RATE_DOMAIN_VERIFY, "org1:other.edu")                       # its own bucket
    await hit_fail_open(redis, clock, RATE_DOMAIN_VERIFY, "org2:amity.edu")                       # per organisation too
    clock.advance(seconds=3601)
    await hit_fail_open(redis, clock, RATE_DOMAIN_VERIFY, "org1:amity.edu")                       # a new hour

    class Down:
        def pipeline(self, *a, **k):
            raise ConnectionError("redis is down")

    await hit_fail_open(Down(), clock, RATE_DOMAIN_VERIFY, "org1:amity.edu")                       # fails open
