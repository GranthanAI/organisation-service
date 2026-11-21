"""The invite.send job: sends after the creating transaction commits, skips stale versions, retries on failure."""

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
from app.security.tokens import hash_token, invite_token
from app.services import invite_jobs, invite_service, job_service
from app.services.permission_rules import Action
from app.workers.job_runner import JobRunner
from tests.support.factories import NOW, make_invite, make_membership, make_org, make_seat_state
from tests.support.fakes import FakeEmailSender


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def mail(monkeypatch):
    fake = FakeEmailSender()
    monkeypatch.setattr(invite_jobs, "get_email_sender", lambda: fake)
    return fake


async def seed(session_factory, **org_kw):
    org = make_org(**org_kw)
    owner = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, seats_bought=50))
        await KnownUserRepository(s).upsert(owner, "dean@amity.edu", "Dean Of Amity", NOW)
        await MembershipRepository(s).add(make_membership(org.id, owner, role="owner"))
    user = CurrentUser(id=owner, email="dean@amity.edu", jti="j")
    return org, OrgContext(org_id=org.id, user=user, role=Role.OWNER, membership_id=uuid.uuid4(), action=Action.INVITE)


async def create(session_factory, ctx, clock, emails=("new@amity.edu",), role="member"):
    async with unit_of_work(session_factory) as s:
        results, _ = await invite_service.create_invites(s, ctx, list(emails), role, clock)
    return [r.invite_id for r in results]


async def run(session_factory, clock):
    return await JobRunner(session_factory, None, clock, poll_seconds=0.05).run_once()


async def invite_row(session_factory, invite_id) -> Invite:
    async with session_factory() as s:
        return await s.get(Invite, invite_id)


async def send_jobs(session_factory):
    async with session_factory() as s:
        return [j for j in (await s.execute(select(Job).order_by(Job.run_at))).scalars() if j.kind == "invite.send"]


async def test_the_email_is_sent_after_the_commit_with_the_derived_link(session_factory, clock, mail):
    org, ctx = await seed(session_factory, name="Amity University")
    (invite_id,) = await create(session_factory, ctx, clock, role="admin")
    assert mail.sent == []                                         # creating queued a job, it sent nothing itself
    assert await run(session_factory, clock) == 1
    (sent,) = mail.sent
    assert sent.to == "new@amity.edu" and sent.subject == "Dean Of Amity invited you to Amity University on GranthanAI"
    token = invite_token(invite_id, 1)
    assert f"{settings.APP_BASE_URL}/join/{token}" in sent.text and f"/join/{token}" in sent.html
    assert "an admin" in sent.text and "(India time)" in sent.text
    row = await invite_row(session_factory, invite_id)
    assert row.last_sent_at == NOW and row.token_hash == hash_token(token)
    (job,) = await send_jobs(session_factory)
    assert job.done_at == NOW


async def test_a_rolled_back_creation_sends_nothing(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    with pytest.raises(RuntimeError):
        async with unit_of_work(session_factory) as s:
            await invite_service.create_invites(s, ctx, ["x@amity.edu"], "member", clock)
            raise RuntimeError("the request failed after queueing")
    assert await send_jobs(session_factory) == [] and await run(session_factory, clock) == 0 and mail.sent == []


async def test_a_stale_version_is_skipped_and_only_the_new_one_sends(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    (invite_id,) = await create(session_factory, ctx, clock)
    async with unit_of_work(session_factory) as s:                 # a resend happens before the first job runs
        await invite_service.resend(s, org.id, invite_id, ctx.user, clock=clock)
    assert len(await send_jobs(session_factory)) == 2
    await run(session_factory, clock)
    assert len(mail.sent) == 1                                     # version 1 was skipped, version 2 sent
    assert mail.tokens() == [invite_token(invite_id, 2)] and invite_token(invite_id, 1) not in mail.sent[0].text
    row = await invite_row(session_factory, invite_id)
    assert row.token_version == 2 and row.last_sent_at == NOW


async def test_an_smtp_failure_retries_with_backoff_and_sends_once(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    (invite_id,) = await create(session_factory, ctx, clock)
    mail.fail_times = 2
    await run(session_factory, clock)
    (job,) = await send_jobs(session_factory)
    assert job.attempts == 1 and job.done_at is None and "ConnectionError" in job.last_error
    assert (await invite_row(session_factory, invite_id)).last_sent_at is None
    assert await run(session_factory, clock) == 0                  # still inside the backoff
    clock.advance(seconds=1)
    await run(session_factory, clock)
    assert (await send_jobs(session_factory))[0].attempts == 2
    clock.advance(seconds=2)
    await run(session_factory, clock)
    assert len(mail.sent) == 1 and mail.attempts == 3
    assert (await send_jobs(session_factory))[0].done_at is not None
    assert (await invite_row(session_factory, invite_id)).last_sent_at == clock.now()


async def test_invite_emails_via_notification_queues_nothing(session_factory, clock, mail, monkeypatch):
    monkeypatch.setattr(settings, "INVITE_EMAILS_VIA", "notification")
    org, ctx = await seed(session_factory)
    await create(session_factory, ctx, clock)
    assert await send_jobs(session_factory) == [] and await run(session_factory, clock) == 0 and mail.sent == []


@pytest.mark.parametrize("change", ["revoked", "accepted", "expired", "purged", "org_suspended"])
async def test_nothing_is_sent_for_an_invite_that_can_no_longer_be_used(session_factory, clock, mail, change):
    org, ctx = await seed(session_factory)
    (invite_id,) = await create(session_factory, ctx, clock)
    async with unit_of_work(session_factory) as s:
        row = await s.get(Invite, invite_id)
        if change == "revoked":
            row.revoked_at, row.revoke_reason = NOW, "revoked"
        elif change == "accepted":
            row.accepted_at, row.accepted_by = NOW, uuid.uuid4()
        elif change == "expired":
            row.expires_at = NOW - timedelta(seconds=1)
        elif change == "purged":
            row.email, row.purged_at = None, NOW
        else:
            (await OrgRepository(s).get(org.id)).status = "suspended"
    await run(session_factory, clock)
    assert mail.sent == []
    assert (await send_jobs(session_factory))[0].done_at is not None          # skipped, not failed


async def test_an_invite_made_before_derived_tokens_gets_its_derived_hash_at_first_send(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    legacy = make_invite(org.id, email="legacy.owner@amity.edu", role="owner", token_hash="random-old-hash-from-phase-5",
                         created_by=uuid.uuid4(), expires_at=NOW + timedelta(days=14))
    async with unit_of_work(session_factory) as s:
        s.add(legacy)
    async with unit_of_work(session_factory) as s:
        await job_service.schedule(s, "invite.send", NOW, {"invite_id": str(legacy.id), "token_version": 1})
    await run(session_factory, clock)
    row = await invite_row(session_factory, legacy.id)
    assert row.token_hash == hash_token(invite_token(legacy.id, 1)) != "random-old-hash-from-phase-5"
    assert mail.tokens() == [invite_token(legacy.id, 1)]
    assert "GranthanAI invited you" in mail.sent[0].subject                  # made by staff, who is not a member: GranthanAI


async def test_the_inviter_who_left_the_org_is_shown_as_an_organisation_admin_or_granthanai(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    (invite_id,) = await create(session_factory, ctx, clock)
    async with unit_of_work(session_factory) as s:
        m = await MembershipRepository(s).get_active(org.id, ctx.user.id)
        m.status, m.ended_at, m.end_reason = "left", NOW, "left"
    await run(session_factory, clock)
    assert mail.sent[0].subject.startswith("GranthanAI invited you")           # no longer a member of the org


async def test_logs_never_contain_the_link_the_token_or_the_address(session_factory, clock, mail):
    org, ctx = await seed(session_factory)
    (invite_id,) = await create(session_factory, ctx, clock, emails=("private.person@amity.edu",))
    from structlog.testing import capture_logs
    with capture_logs() as logs:
        await run(session_factory, clock)
    token = invite_token(invite_id, 1)
    text = repr(logs)
    assert "invite email sent" in text
    assert token not in text and "private.person@amity.edu" not in text and "/join/" not in text
