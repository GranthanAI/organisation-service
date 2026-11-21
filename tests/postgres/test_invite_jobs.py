"""invites.expire and invites.purge: daily housekeeping in batches of 500."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core import constants
from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.models.invite import Invite
from app.repositories.org_repository import OrgRepository
from app.services import invite_jobs
from app.services.job_handlers import RECURRING
from tests.support.factories import NOW, make_invite, make_org


@pytest.fixture
def clock():
    return FixedClock(NOW)


async def seed_org(session_factory):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
    return org


async def add(session_factory, org, n=1, **kw):
    async with unit_of_work(session_factory) as s:
        for _ in range(n):
            s.add(make_invite(org.id, email=f"{uuid.uuid4().hex[:12]}@amity.edu", **kw))


async def run(session_factory, handler, clock):
    async with unit_of_work(session_factory) as s:
        await handler(s, None, clock)


async def count(session_factory, *where):
    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(Invite).where(*where))).scalar_one()


def test_both_jobs_are_registered_as_daily_housekeeping():
    kinds = {r.kind if hasattr(r, "kind") else r[0] for r in RECURRING} if not isinstance(RECURRING, dict) else set(RECURRING)
    assert {"invites.expire", "invites.purge"} <= kinds


async def test_expire_closes_only_open_invites_past_their_expiry(session_factory, clock):
    org = await seed_org(session_factory)
    await add(session_factory, org, expires_at=NOW - timedelta(seconds=1))                       # open, late
    await add(session_factory, org, expires_at=NOW)                                              # exactly now: late
    await add(session_factory, org, expires_at=NOW + timedelta(seconds=1))                       # still open
    await add(session_factory, org, expires_at=NOW - timedelta(days=1), revoked_at=NOW - timedelta(days=2),
              revoke_reason="revoked")                                                           # already revoked: untouched
    await add(session_factory, org, expires_at=NOW - timedelta(days=1), accepted_at=NOW - timedelta(days=3),
              accepted_by=uuid.uuid4())                                                          # accepted: untouched
    await run(session_factory, invite_jobs.invites_expire, clock)
    assert await count(session_factory, Invite.revoke_reason == "expired", Invite.revoked_at == NOW) == 2
    assert await count(session_factory, Invite.revoked_at.is_(None), Invite.accepted_at.is_(None)) == 1
    assert await count(session_factory, Invite.revoke_reason == "revoked", Invite.revoked_at == NOW - timedelta(days=2)) == 1
    assert await count(session_factory, Invite.accepted_at.is_not(None), Invite.revoked_at.is_(None)) == 1


async def test_expire_works_in_batches_and_gets_through_more_than_500(session_factory, clock):
    org = await seed_org(session_factory)
    total = constants.INVITE_JOB_BATCH * 2 + 37
    await add(session_factory, org, n=total, expires_at=NOW - timedelta(hours=1))
    await run(session_factory, invite_jobs.invites_expire, clock)
    assert await count(session_factory, Invite.revoke_reason == "expired") == total
    assert await count(session_factory, Invite.revoked_at.is_(None)) == 0


async def test_expire_is_idempotent(session_factory, clock):
    org = await seed_org(session_factory)
    await add(session_factory, org, n=3, expires_at=NOW - timedelta(hours=1))
    await run(session_factory, invite_jobs.invites_expire, clock)
    clock.advance(seconds=3600)
    await run(session_factory, invite_jobs.invites_expire, clock)                                 # nothing left to do
    assert await count(session_factory, Invite.revoked_at == NOW) == 3                            # revoked_at not rewritten


async def test_purge_blanks_the_email_of_old_never_accepted_invites(session_factory, clock):
    org = await seed_org(session_factory)
    old = NOW - timedelta(days=constants.INVITE_PURGE_AFTER_DAYS + 1)
    await add(session_factory, org, created_at=old)                                              # old, open
    await add(session_factory, org, created_at=old, revoked_at=old, revoke_reason="expired")     # old, closed
    await add(session_factory, org, created_at=NOW - timedelta(days=constants.INVITE_PURGE_AFTER_DAYS - 1))   # young
    await add(session_factory, org, created_at=old, accepted_at=old, accepted_by=uuid.uuid4())  # accepted: kept
    await run(session_factory, invite_jobs.invites_purge, clock)
    assert await count(session_factory, Invite.purged_at == NOW, Invite.email.is_(None)) == 2
    assert await count(session_factory, Invite.purged_at.is_(None), Invite.email.is_not(None)) == 2
    assert await count(session_factory, Invite.accepted_at.is_not(None), Invite.email.is_not(None)) == 1


async def test_purge_exactly_ninety_days_old_is_kept_and_the_day_after_is_not(session_factory, clock):
    org = await seed_org(session_factory)
    await add(session_factory, org, created_at=NOW - timedelta(days=constants.INVITE_PURGE_AFTER_DAYS))
    await run(session_factory, invite_jobs.invites_purge, clock)
    assert await count(session_factory, Invite.purged_at.is_not(None)) == 0
    clock.advance(seconds=1)
    await run(session_factory, invite_jobs.invites_purge, clock)
    assert await count(session_factory, Invite.purged_at.is_not(None)) == 1


async def test_purge_in_batches_and_never_twice(session_factory, clock):
    org = await seed_org(session_factory)
    total = constants.INVITE_JOB_BATCH + 120
    await add(session_factory, org, n=total, created_at=NOW - timedelta(days=100))
    await run(session_factory, invite_jobs.invites_purge, clock)
    assert await count(session_factory, Invite.purged_at == NOW) == total
    clock.advance(seconds=86400)
    await run(session_factory, invite_jobs.invites_purge, clock)
    assert await count(session_factory, Invite.purged_at == NOW) == total                       # purged_at is not rewritten


async def test_a_purged_invite_can_never_be_used_again(session_factory, clock):
    from app.security.tokens import invite_token_hash
    org = await seed_org(session_factory)
    invite_id = uuid.uuid4()
    await add(session_factory, org, id=invite_id, created_at=NOW - timedelta(days=100),
              token_hash=invite_token_hash(invite_id, 1))
    await run(session_factory, invite_jobs.invites_purge, clock)
    async with session_factory() as s:
        row = await s.get(Invite, invite_id)
    assert row.email is None and row.purged_at == NOW
