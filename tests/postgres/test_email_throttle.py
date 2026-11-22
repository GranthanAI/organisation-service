"""N1: the global email send rate. 61 queued sends in one minute give 60 sent and 1 deferred to the next minute."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.config import settings
from app.core.enums import Role
from app.db.session import unit_of_work
from app.models.invite import Invite
from app.models.job import Job
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser, OrgContext
from app.services import email_throttle, invite_jobs, invite_service
from app.services.permission_rules import Action
from app.telemetry.metrics import EMAIL_DEFERRED, EMAIL_SENT
from app.workers.job_runner import JobRunner
from tests.support.factories import NOW, make_membership, make_org, make_seat_state
from tests.support.fakes import FakeEmailSender


@pytest.fixture
def clock():
    return FixedClock(NOW.replace(second=5))          # inside a minute, with time left in it


@pytest.fixture
def mail(monkeypatch):
    fake = FakeEmailSender()
    monkeypatch.setattr(invite_jobs, "get_email_sender", lambda: fake)
    return fake


async def seed(session_factory, clock, n):
    org, owner = make_org(), uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, seats_bought=500))
        await KnownUserRepository(s).upsert(owner, "dean@amity.edu", "Dean", NOW)
        await MembershipRepository(s).add(make_membership(org.id, owner, role="owner"))
    ctx = OrgContext(org_id=org.id, user=CurrentUser(id=owner, email="dean@amity.edu", jti="j"), role=Role.OWNER,
                     membership_id=uuid.uuid4(), action=Action.INVITE)
    async with unit_of_work(session_factory) as s:
        await invite_service.create_invites(s, ctx, [f"s{i}@amity.edu" for i in range(n)], "member", clock)


async def drain(session_factory, clock):
    runner, total = JobRunner(session_factory, None, clock, poll_seconds=0.05), 0
    while (ran := await runner.run_once()):
        total += ran
        if ran < 10:
            break
    return total


async def sends(session_factory):
    async with session_factory() as s:
        return [j for j in (await s.execute(select(Job))).scalars() if j.kind == "invite.send"]


async def test_61_queued_sends_give_60_sent_and_1_deferred_to_the_next_minute(session_factory, clock, mail, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_SENDS_PER_MINUTE", 60)
    await seed(session_factory, clock, 50)
    await seed(session_factory, clock, 11)                         # 61 sends queued in all (different orgs)
    sent0, deferred0 = EMAIL_SENT._value.get(), EMAIL_DEFERRED._value.get()
    while await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once():
        pass
    assert len(mail.sent) == 60
    open_jobs = [j for j in await sends(session_factory) if j.done_at is None]
    assert len(open_jobs) == 1
    next_minute = (clock.now().replace(second=0, microsecond=0)) + timedelta(minutes=1)
    assert open_jobs[0].run_at == next_minute
    assert EMAIL_SENT._value.get() - sent0 == 60 and EMAIL_DEFERRED._value.get() - deferred0 >= 1
    clock.advance(seconds=60)                                      # the next minute: a fresh counter
    while await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once():
        pass
    assert len(mail.sent) == 61 and all(j.done_at for j in await sends(session_factory))


async def test_a_deferral_is_not_an_attempt_or_a_failure(session_factory, clock, mail, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_SENDS_PER_MINUTE", 1)
    await seed(session_factory, clock, 3)
    for _ in range(3):
        await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once()
    assert len(mail.sent) == 1
    deferred = [j for j in await sends(session_factory) if j.done_at is None]
    assert len(deferred) == 2 and all(j.attempts == 0 and j.last_error is None and j.locked_until is None for j in deferred)
    for minute in range(2):                                        # one more email each minute, never a failure
        clock.advance(seconds=60)
        while await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once():
            pass
    assert len(mail.sent) == 3 and all(j.attempts == 0 for j in await sends(session_factory))


async def test_a_skipped_invite_does_not_use_a_send_slot(session_factory, clock, mail, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_SENDS_PER_MINUTE", 1)
    await seed(session_factory, clock, 2)
    async with unit_of_work(session_factory) as s:                 # one of the two is revoked before it is sent
        first = (await s.execute(select(Invite).order_by(Invite.email))).scalars().first()
        first.revoked_at, first.revoke_reason = NOW, "revoked"
    while await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once():
        pass
    assert len(mail.sent) == 1 and all(j.done_at for j in await sends(session_factory))


async def test_redis_down_sends_one_a_second_in_this_process(session_factory, clock, mail, monkeypatch, fake_throttle_redis):
    class Down:
        def pipeline(self, *a, **k):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(email_throttle, "get_redis", lambda: Down())
    await seed(session_factory, clock, 3)
    await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once()
    assert len(mail.sent) == 1                                     # mail does not stop completely
    pending = [j for j in await sends(session_factory) if j.done_at is None]
    assert len(pending) == 2 and all(j.attempts == 0 for j in pending)
    assert {j.run_at for j in pending} == {clock.now() + timedelta(seconds=1)}
    clock.advance(seconds=1)
    await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once()
    assert len(mail.sent) == 2
    clock.advance(seconds=1)
    await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once()
    assert len(mail.sent) == 3


async def test_the_window_key_and_expiry(clock, fake_throttle_redis):
    assert await email_throttle.acquire(clock) is None
    minute = int(clock.now().timestamp()) // 60
    assert int(await fake_throttle_redis.get(f"org:email_send:{minute}")) == 1
    assert 0 < await fake_throttle_redis.ttl(f"org:email_send:{minute}") <= 120
