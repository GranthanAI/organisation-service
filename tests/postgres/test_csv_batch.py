"""A 2,000-row college file: 50 bad rows give 1,950 invites and 50 errors; sending finishes and the batch becomes done."""

import uuid
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.clock import FixedClock
from app.core.config import settings
from app.core.enums import Role
from app.db.session import unit_of_work
from app.models.invite import Invite, InviteBatch
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser, OrgContext
from app.services import csv_service, invite_jobs
from app.services.permission_rules import Action
from app.workers.job_runner import JobRunner
from tests.support.factories import NOW, make_membership, make_org, make_seat_state
from tests.support.fakes import FakeEmailSender

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "college_2000.csv"


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture(autouse=True)
def mail(monkeypatch):
    fake = FakeEmailSender()
    monkeypatch.setattr(invite_jobs, "get_email_sender", lambda: fake)
    monkeypatch.setattr(settings, "EMAIL_SENDS_PER_MINUTE", 100_000)       # the throttle is tested in test_email_throttle
    return fake


async def seed(session_factory):
    org, owner = make_org(name="Amity University"), uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, seats_bought=5000))
        await KnownUserRepository(s).upsert(owner, "dean@amity.edu", "Dean", NOW)
        await MembershipRepository(s).add(make_membership(org.id, owner, role="owner"))
    ctx = OrgContext(org_id=org.id, user=CurrentUser(id=owner, email="dean@amity.edu", jti="j"), role=Role.OWNER,
                     membership_id=uuid.uuid4(), action=Action.INVITE)
    return org, ctx


async def upload_fixture(session_factory, ctx, clock):
    async with unit_of_work(session_factory) as s:
        return await csv_service.upload(s, ctx, FIXTURE.name, FIXTURE.read_bytes(), "member", False, clock)


async def progress(session_factory, batch_id, clock):
    async with session_factory() as s:
        batch = await s.get(InviteBatch, batch_id)
        return batch, await csv_service.progress(s, batch, clock.now())


async def finish(session_factory, clock):
    async with unit_of_work(session_factory) as s:
        await invite_jobs.csv_batches_finish(s, None, clock)


async def test_the_2000_row_file_gives_1950_invites_and_50_errors(session_factory, clock):
    org, ctx = await seed(session_factory)
    report = await upload_fixture(session_factory, ctx, clock)
    assert (report.rows_total, report.rows_ok, report.rows_failed) == (2000, 1950, 50)
    assert Counter(e["reason"] for e in report.errors) == {
        "invalid_email": 25, "duplicate_in_file": 13, "role_not_allowed": 12}
    async with session_factory() as s:
        n = lambda model, *w: s.execute(select(func.count()).select_from(model).where(*w))   # noqa: E731
        assert (await n(Invite, Invite.kind == "csv")).scalar_one() == 1950
        assert (await n(OutboxEvent, OutboxEvent.topic == "org.invite.created")).scalar_one() == 1950
        assert (await n(Job, Job.kind == "invite.send")).scalar_one() == 1950
        batch = await s.get(InviteBatch, report.batch_id)
    assert (batch.status, batch.rows_ok, batch.rows_failed, len(batch.errors)) == ("sending", 1950, 50, 50)
    assert [e["row"] for e in batch.errors] == sorted(e["row"] for e in batch.errors)


async def test_sending_finishes_and_the_batch_becomes_done(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    report = await upload_fixture(session_factory, ctx, clock)
    runner = JobRunner(session_factory, None, clock, poll_seconds=0.05)

    batch, (sent, pending) = await progress(session_factory, report.batch_id, clock)
    assert (batch.status, sent, pending) == ("sending", 0, 1950)
    await finish(session_factory, clock)
    assert (await progress(session_factory, report.batch_id, clock))[0].status == "sending"      # nothing sent yet

    for _ in range(5):                                                       # 5 runs of 10 jobs: 50 emails out
        await runner.run_once()
    batch, (sent, pending) = await progress(session_factory, report.batch_id, clock)
    assert (sent, pending, len(mail.sent)) == (50, 1900, 50) and sent + pending == 1950
    await finish(session_factory, clock)
    assert (await progress(session_factory, report.batch_id, clock))[0].status == "sending"      # still going

    while await runner.run_once():
        pass
    assert len(mail.sent) == 1950 and len({m.to for m in mail.sent}) == 1950
    batch, (sent, pending) = await progress(session_factory, report.batch_id, clock)
    assert (sent, pending, batch.status) == (1950, 0, "sending")             # all sent; the minute job has not run yet
    await finish(session_factory, clock)
    batch, _ = await progress(session_factory, report.batch_id, clock)
    assert batch.status == "done" and batch.finished_at == clock.now()
    await finish(session_factory, clock)                                     # idempotent: finished_at is not rewritten
    clock.advance(seconds=60)
    await finish(session_factory, clock)
    assert (await progress(session_factory, report.batch_id, clock))[0].finished_at == NOW


async def test_a_revoked_or_expired_invite_does_not_hold_the_batch_open(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    async with unit_of_work(session_factory) as s:
        report = await csv_service.upload(s, ctx, "a.csv", b"email\na@amity.edu\nb@amity.edu\nc@amity.edu\n", "member", False, clock)
    async with unit_of_work(session_factory) as s:
        rows = list((await s.execute(select(Invite).order_by(Invite.email))).scalars())
        rows[0].revoked_at, rows[0].revoke_reason = NOW, "revoked"            # a is revoked before it is sent
        from datetime import timedelta
        rows[1].expires_at = NOW - timedelta(seconds=1)                       # b expires before it is sent
    while await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once():
        pass
    assert [m.to for m in mail.sent] == ["c@amity.edu"]
    _, (sent, pending) = await progress(session_factory, report.batch_id, clock)
    assert (sent, pending) == (1, 0)
    await finish(session_factory, clock)
    assert (await progress(session_factory, report.batch_id, clock))[0].status == "done"


async def test_a_batch_with_a_waiting_invite_stays_sending(session_factory, clock):
    org, ctx = await seed(session_factory)
    async with unit_of_work(session_factory) as s:
        report = await csv_service.upload(s, ctx, "a.csv", b"email\na@amity.edu\n", "member", False, clock)
    clock.advance(seconds=3600)                                              # an hour passes, nobody ran the jobs
    await finish(session_factory, clock)
    batch, (sent, pending) = await progress(session_factory, report.batch_id, clock)
    assert (batch.status, sent, pending) == ("sending", 0, 1)


async def test_a_dry_run_batch_is_never_touched_by_the_finish_job(session_factory, clock):
    org, ctx = await seed(session_factory)
    async with unit_of_work(session_factory) as s:
        report = await csv_service.upload(s, ctx, "a.csv", b"email\na@amity.edu\n", "member", True, clock)
    clock.advance(seconds=60)
    await finish(session_factory, clock)
    batch, (sent, pending) = await progress(session_factory, report.batch_id, clock)
    assert (batch.status, batch.finished_at, sent, pending) == ("done", NOW, 0, 0)


async def test_the_fixture_is_what_the_generator_writes(tmp_path):
    from scripts import make_csv_fixture
    out = tmp_path / "college_2000.csv"
    make_csv_fixture.main(out)
    assert out.read_bytes() == FIXTURE.read_bytes()
    assert FIXTURE.read_bytes().startswith(b"\xef\xbb\xbf") and FIXTURE.read_bytes().count(b"\r\n") == 2001
