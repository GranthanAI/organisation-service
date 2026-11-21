"""pilot.remind and pilot.expire with a FixedClock; extend and conversion interplay."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.config import settings
from app.core.enums import JoinSource, StaffRole
from app.core.exceptions import OrgInactive
from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.job import Job
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.outbox import OutboxEvent
from app.security.models import CurrentUser
from app.services import directory_service, join_service, org_service, seat_service
from app.workers.job_runner import JobRunner
from tests.support.factories import NOW, new_known

STAFF = CurrentUser(id=uuid.UUID(int=7), email="staff@granthan.com", jti="j", roles=["admin"], staff_role=StaffRole.ADMIN)
REASON = "pilot for the college"


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture(autouse=True)
def no_invite_emails(monkeypatch):
    """These tests are about pilot jobs: the owner invite of a new org should not queue an email job of its own."""
    monkeypatch.setattr(settings, "INVITE_EMAILS_VIA", "notification")


async def new_pilot(session_factory, clock, name="Amity University"):
    async with unit_of_work(session_factory) as s:
        created = await org_service.create_org(
            s, STAFF, name=name, type="college", billing_email="a@amity.edu", owner_email="dean@amity.edu", pilot=True,
            reason=REASON, clock=clock)
    return created.org


async def run_due(session_factory, clock):
    return await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once()


async def org_row(session_factory, org) -> Organisation:
    async with session_factory() as s:
        return await s.get(Organisation, org.id)


async def events(session_factory, topic="org.pilot.ending"):
    async with session_factory() as s:
        return [e for e in (await s.execute(select(OutboxEvent).order_by(OutboxEvent.created_at))).scalars()
                if e.topic == topic]


async def jobs(session_factory, kind=None, open_only=False):
    async with session_factory() as s:
        rows = list((await s.execute(select(Job).order_by(Job.run_at))).scalars())
    return [j for j in rows if (kind is None or j.kind == kind) and (not open_only or j.done_at is None)]


# ---------------------------------------------------------------- remind
async def test_nothing_happens_before_the_first_reminder(session_factory, clock):
    await new_pilot(session_factory, clock)
    clock.advance(days=22, hours=23)                                  # a little before ends-7d
    assert await run_due(session_factory, clock) == 0
    assert await events(session_factory) == []


async def test_the_reminder_at_seven_days_and_the_one_at_one_day(session_factory, clock):
    org = await new_pilot(session_factory, clock)
    end = NOW + timedelta(days=30)

    clock.set(end - timedelta(days=7))
    assert await run_due(session_factory, clock) == 1
    (seven,) = await events(session_factory)
    assert seven.key == str(org.id)                                    # keyed by org_id
    assert seven.payload["org_id"] == str(org.id) and seven.payload["days_left"] == 7
    assert seven.payload["ends_at"] == "2026-12-21T12:00:00Z" and seven.payload["schema_version"] == "1.0"

    clock.set(end - timedelta(days=1))
    assert await run_due(session_factory, clock) == 1
    one = (await events(session_factory))[1]
    assert one.payload["days_left"] == 1
    assert len(await events(session_factory)) == 2


async def test_each_reminder_is_sent_once(session_factory, clock):
    await new_pilot(session_factory, clock)
    clock.set(NOW + timedelta(days=23))
    await run_due(session_factory, clock)
    await run_due(session_factory, clock)
    clock.advance(hours=3)
    await run_due(session_factory, clock)
    assert len(await events(session_factory)) == 1


async def test_a_pilot_shorter_than_seven_days_only_gets_the_one_day_reminder(session_factory, clock, monkeypatch):
    monkeypatch.setattr(settings, "PILOT_DAYS", 5)
    org = await new_pilot(session_factory, clock)
    open_jobs = {j.dedupe_key for j in await jobs(session_factory, open_only=True)}
    assert f"pilot.remind:{org.id}:7d" not in open_jobs and f"pilot.remind:{org.id}:1d" in open_jobs


@pytest.mark.parametrize("change", ["suspend", "close", "convert"])
async def test_a_reminder_is_skipped_once_the_org_is_no_longer_a_pilot(session_factory, clock, change):
    org = await new_pilot(session_factory, clock)
    async with unit_of_work(session_factory) as s:
        if change == "suspend":
            await org_service.suspend(s, STAFF, org.id, REASON, clock)
        elif change == "close":
            await org_service.close(s, STAFF, org.id, REASON, clock)
        else:
            await seat_service.set_from_staff(s, org.id, "org_pro", "college", 60, None, STAFF, REASON, clock=clock)
    clock.set(NOW + timedelta(days=23))
    await run_due(session_factory, clock)
    assert await events(session_factory) == []


async def test_a_stale_reminder_is_skipped_when_the_pilot_was_extended_meanwhile(session_factory, clock):
    from app.services.org_jobs import pilot_remind
    org = await new_pilot(session_factory, clock)
    clock.set(NOW + timedelta(days=23))
    async with unit_of_work(session_factory) as s:
        row = await s.get(Organisation, org.id)
        row.pilot_ends_at = clock.now() + timedelta(days=20)             # extended, but this old job still fires
        job = Job(id=uuid.uuid4(), kind="pilot.remind", run_at=clock.now(), payload={"org_id": str(org.id), "days_left": 7})
        await pilot_remind(s, job, clock)
    assert await events(session_factory) == []


# ---------------------------------------------------------------- expire
async def test_expire_moves_a_pilot_to_pilot_expired_and_audits_it(session_factory, clock):
    org = await new_pilot(session_factory, clock)
    clock.set(NOW + timedelta(days=30))
    await run_due(session_factory, clock)
    assert (await org_row(session_factory, org)).status == "pilot_expired"
    async with session_factory() as s:
        change = [a for a in (await s.execute(select(AuditLog))).scalars() if a.action == "org.status_changed"][-1]
    assert (change.actor_kind, change.org_id) == ("system", org.id)
    assert change.details["from"] == "pilot" and change.details["to"] == "pilot_expired"


async def test_members_stay_members_but_joins_are_refused_and_billing_stops_granting(session_factory, clock):
    org = await new_pilot(session_factory, clock)
    async with unit_of_work(session_factory) as s:
        member = (await org_service.add_member(s, STAFF, org.id, await new_known(session_factory), "member", REASON, clock)).user_id
    clock.set(NOW + timedelta(days=30))
    await run_due(session_factory, clock)

    async with session_factory() as s:
        actives = [m for m in (await s.execute(select(Membership))).scalars() if m.status == "active"]
    assert [m.user_id for m in actives] == [member]                              # the member is still a member
    with pytest.raises(OrgInactive):
        async with unit_of_work(session_factory) as s:
            await join_service.join(s, org.id, await new_known(session_factory), JoinSource.STAFF, actor="system", clock=clock)
    async with session_factory() as s:
        assert (await directory_service.membership_for_user(s, member))["status"] == "pilot_expired"


async def test_expire_waits_for_the_end_time(session_factory, clock):
    org = await new_pilot(session_factory, clock)
    clock.set(NOW + timedelta(days=30) - timedelta(minutes=1))
    await run_due(session_factory, clock)
    assert (await org_row(session_factory, org)).status == "pilot"


@pytest.mark.parametrize("change", ["convert", "suspend", "close"])
async def test_expire_is_skipped_after_a_conversion_suspension_or_close(session_factory, clock, change):
    org = await new_pilot(session_factory, clock)
    async with unit_of_work(session_factory) as s:
        if change == "convert":
            await seat_service.set_from_staff(s, org.id, "payg", "college", None, None, STAFF, REASON, clock=clock)
        elif change == "suspend":
            await org_service.suspend(s, STAFF, org.id, REASON, clock)
        else:
            await org_service.close(s, STAFF, org.id, REASON, clock)
    expected = {"convert": "active", "suspend": "suspended", "close": "closed"}[change]
    clock.set(NOW + timedelta(days=31))
    await run_due(session_factory, clock)
    assert (await org_row(session_factory, org)).status == expected


async def test_expire_without_a_job_payload_org_is_a_no_op(session_factory, clock):
    from app.services.org_jobs import pilot_expire
    async with unit_of_work(session_factory) as s:
        job = Job(id=uuid.uuid4(), kind="pilot.expire", run_at=NOW, payload={"org_id": str(uuid.uuid4())})
        await pilot_expire(s, job, clock)          # an unknown org: nothing to do, no error


# ---------------------------------------------------------------- extend
async def test_extend_replaces_the_open_jobs_so_the_old_end_does_not_expire_the_org(session_factory, clock):
    org = await new_pilot(session_factory, clock)
    old_keys = {j.dedupe_key: j.id for j in await jobs(session_factory, open_only=True)}
    clock.advance(days=20)
    async with unit_of_work(session_factory) as s:
        await org_service.extend_pilot(s, STAFF, org.id, 15, REASON, clock)        # new end: day 45
    new_jobs = {j.dedupe_key: j for j in await jobs(session_factory, open_only=True)}
    assert set(new_jobs) == set(old_keys)                                          # same keys, replaced rows
    assert all(new_jobs[k].id != old_keys[k] for k in old_keys)
    end = NOW + timedelta(days=45)
    assert new_jobs[f"pilot.expire:{org.id}"].run_at == end
    assert new_jobs[f"pilot.remind:{org.id}:7d"].run_at == end - timedelta(days=7)

    clock.set(NOW + timedelta(days=31))                                            # the OLD end has passed
    await run_due(session_factory, clock)
    assert (await org_row(session_factory, org)).status == "pilot"                 # still a pilot

    clock.set(end)
    await run_due(session_factory, clock)
    assert (await org_row(session_factory, org)).status == "pilot_expired"


async def test_a_reminder_that_already_went_is_not_sent_again_after_an_extension(session_factory, clock):
    org = await new_pilot(session_factory, clock)
    clock.set(NOW + timedelta(days=23))
    await run_due(session_factory, clock)                                          # the 7-day reminder
    async with unit_of_work(session_factory) as s:
        await org_service.extend_pilot(s, STAFF, org.id, 10, REASON, clock)
    sent = await events(session_factory)
    assert len(sent) == 1 and sent[0].payload["days_left"] == 7
    open_remind = [j for j in await jobs(session_factory, "pilot.remind", open_only=True)]
    assert {j.payload["days_left"] for j in open_remind} == {7, 1}                 # new ones for the new end
