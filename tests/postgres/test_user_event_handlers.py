"""The auth-service event handlers, against a real database: upsert, late events, earliest verification, user.deleted."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.events.schemas import EmailVerifiedPayload, UserDeletedEvent, UserRegisteredPayload
from app.models.audit_log import AuditLog
from app.models.invite import Invite
from app.models.known_user import KnownUser
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.models.processed_event import ProcessedEvent
from app.repositories.invite_repository import InviteRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import user_event_handlers as h
from tests.support.factories import NOW, make_invite, make_membership, make_org, make_seat_state

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
USER = uuid.UUID("0194b7a2-5f3e-7c11-8a41-2d3c4b5a6e70")


@pytest.fixture
def clock():
    return FixedClock(NOW)


def registered(**kw) -> UserRegisteredPayload:
    body = json.loads((FIXTURES / "user_event_registered.json").read_text())["payload"]
    return UserRegisteredPayload.model_validate({**body, **kw})


def verified(**kw) -> EmailVerifiedPayload:
    body = json.loads((FIXTURES / "user_event_email_verified.json").read_text())["payload"]
    return EmailVerifiedPayload.model_validate({**body, **kw})


def deleted(user=USER, **kw) -> UserDeletedEvent:
    body = json.loads((FIXTURES / "user_deleted.json").read_text())
    return UserDeletedEvent.model_validate({**body, "user_id": str(user), "event_id": str(uuid.uuid4()), **kw})


async def known(session_factory, user=USER) -> KnownUser | None:
    async with session_factory() as s:
        return await s.get(KnownUser, user)


async def all_rows(session_factory, model):
    async with session_factory() as s:
        return list((await s.execute(select(model))).scalars())


# ---------------------------------------------------------------- UserRegistered
async def test_user_registered_upserts_known_users(session_factory, clock):
    assert await h.process_user_registered(session_factory, registered(), clock) is True
    row = await known(session_factory)
    assert row.email == "ada.lovelace@amity.edu"                      # normalised
    assert (row.full_name, row.email_verified_at, row.deleted_at) == ("Ada Lovelace", None, None)
    assert row.updated_at == NOW

    clock.advance(minutes=5)
    await h.process_user_registered(session_factory, registered(event_id=str(uuid.uuid4()), full_name="Ada King"), clock)
    row = await known(session_factory)
    assert row.full_name == "Ada King" and row.updated_at == NOW + timedelta(minutes=5)
    assert len(await all_rows(session_factory, KnownUser)) == 1


async def test_user_registered_never_clears_email_verified_at(session_factory, clock):
    await h.process_email_verified(session_factory, verified(), clock)
    stamp = (await known(session_factory)).email_verified_at
    assert stamp is not None
    await h.process_user_registered(session_factory, registered(event_id=str(uuid.uuid4())), clock)
    assert (await known(session_factory)).email_verified_at == stamp


async def test_a_late_user_registered_after_deletion_is_ignored(session_factory, clock):
    await h.process_user_registered(session_factory, registered(), clock)
    await h.process_user_deleted(session_factory, deleted(), clock)
    before = await known(session_factory)
    assert before.deleted_at == NOW

    clock.advance(hours=1)
    await h.process_user_registered(session_factory, registered(event_id=str(uuid.uuid4()), full_name="Back Again",
                                                                email="new@amity.edu"), clock)
    after = await known(session_factory)
    assert (after.deleted_at, after.full_name, after.email, after.updated_at) == (
        before.deleted_at, before.full_name, before.email, before.updated_at)


async def test_a_bad_email_is_an_error_not_a_silent_skip(session_factory, clock):
    from app.core.exceptions import InvalidEmail
    with pytest.raises(InvalidEmail):
        await h.process_user_registered(session_factory, registered(email="not-an-email"), clock)
    assert await all_rows(session_factory, ProcessedEvent) == []      # nothing committed, so a retry is possible


# ---------------------------------------------------------------- EmailVerified
async def test_email_verified_sets_the_time_and_updates_the_email(session_factory, clock):
    await h.process_user_registered(session_factory, registered(), clock)
    when = datetime(2026, 11, 21, 12, 5, tzinfo=timezone.utc)
    await h.process_email_verified(session_factory, verified(timestamp=when.isoformat(), email="Ada@Amity.EDU"), clock)
    row = await known(session_factory)
    assert row.email_verified_at == when and row.email == "ada@amity.edu" and row.full_name == "Ada Lovelace"


async def test_email_verified_keeps_the_earliest_time(session_factory, clock):
    early, late = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc), datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
    await h.process_email_verified(session_factory, verified(event_id="e-1", timestamp=early.isoformat()), clock)
    await h.process_email_verified(session_factory, verified(event_id="e-2", timestamp=late.isoformat()), clock)
    assert (await known(session_factory)).email_verified_at == early            # a later event does not move it forward
    fresh = uuid.uuid4()
    await h.process_email_verified(session_factory, verified(event_id="e-3", user_id=str(fresh),
                                                             timestamp=late.isoformat()), clock)
    await h.process_email_verified(session_factory, verified(event_id="e-4", user_id=str(fresh),
                                                             timestamp=early.isoformat()), clock)
    assert (await known(session_factory, fresh)).email_verified_at == early    # an out-of-order earlier one wins


async def test_email_verified_for_an_unknown_user_creates_the_row(session_factory, clock):
    await h.process_email_verified(session_factory, verified(), clock)
    row = await known(session_factory)
    assert row.email == "ada.lovelace@amity.edu" and row.full_name is None and row.email_verified_at is not None


async def test_email_verified_for_a_deleted_user_is_ignored(session_factory, clock):
    await h.process_user_registered(session_factory, registered(), clock)
    await h.process_user_deleted(session_factory, deleted(), clock)
    await h.process_email_verified(session_factory, verified(), clock)
    assert (await known(session_factory)).email_verified_at is None


async def test_the_phase_9_hook_is_called_once_per_verified_event_and_does_nothing_yet(session_factory, clock, monkeypatch):
    calls = []

    async def spy(session, user_id, email):
        calls.append((user_id, email))

    monkeypatch.setattr(h, "on_email_verified", spy)
    await h.process_email_verified(session_factory, verified(), clock)
    await h.process_email_verified(session_factory, verified(), clock)          # a duplicate: not called again
    assert calls == [(USER, "ada.lovelace@amity.edu")]
    assert await h.on_email_verified(None, USER, "x@y.co") is None


# ---------------------------------------------------------------- user.deleted
async def seed_org(session_factory, member_email="ada@amity.edu", roles=("owner", "member")):
    org = make_org()
    people = {}
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id))
        for role in roles:
            people[role] = uuid.uuid4()
            await MembershipRepository(s).add(make_membership(org.id, people[role], role=role))
    return org, people


async def test_user_deleted_ends_the_membership_emits_left_and_marks_the_user(session_factory, clock):
    org, people = await seed_org(session_factory)
    member = people["member"]
    await h.process_user_registered(session_factory, registered(user_id=str(member), email="ada@amity.edu"), clock)

    assert await h.process_user_deleted(session_factory, deleted(member), clock) is True

    row = await known(session_factory, member)
    assert row.deleted_at == NOW
    async with session_factory() as s:
        ended = (await s.execute(select(Membership).where(Membership.user_id == member))).scalar_one()
    assert (ended.status, ended.end_reason, ended.ended_at) == ("removed", "user_deleted", NOW)
    (event,) = await all_rows(session_factory, OutboxEvent)
    assert event.topic == "org.member.left" and event.key == str(member)
    assert event.payload["reason"] == "user_deleted" and event.payload["org_type"] == "college"
    actions = [a.action for a in await all_rows(session_factory, AuditLog)]
    assert actions == ["member.removed"]                                           # the owner is still there


async def test_user_deleted_revokes_open_invites_to_that_email_only(session_factory, clock):
    org, people = await seed_org(session_factory)
    member = people["member"]
    await h.process_user_registered(session_factory, registered(user_id=str(member), email="ada@amity.edu"), clock)
    other_org, _ = await seed_org(session_factory)
    third_org, _ = await seed_org(session_factory)
    async with unit_of_work(session_factory) as s:
        repo = InviteRepository(s)
        open_here = await repo.add(make_invite(org.id, email="ada@amity.edu"))
        open_there = await repo.add(make_invite(other_org.id, email="ada@amity.edu"))
        csv = await repo.add(make_invite(third_org.id, kind="csv", email="ada@amity.edu", token_hash="csv-1"))
        accepted = await repo.add(make_invite(org.id, email="ada@amity.edu", accepted_at=NOW - timedelta(days=1)))
        already = await repo.add(make_invite(other_org.id, email="ada@amity.edu", revoked_at=NOW - timedelta(days=2),
                                             revoke_reason="expired"))
        stranger = await repo.add(make_invite(org.id, email="someone.else@amity.edu"))
        link = await repo.add(make_invite(org.id, kind="link", email=None, token_hash="link-1"))

    await h.process_user_deleted(session_factory, deleted(member), clock)

    async with session_factory() as s:
        got = {i.id: i for i in (await s.execute(select(Invite))).scalars()}
    for revoked in (open_here, open_there, csv):
        assert (got[revoked.id].revoked_at, got[revoked.id].revoke_reason) == (NOW, "user_deleted")
    assert got[accepted.id].revoked_at is None                                    # accepted: left alone
    assert (got[already.id].revoked_at, got[already.id].revoke_reason) == (NOW - timedelta(days=2), "expired")
    assert got[stranger.id].revoked_at is None and got[link.id].revoked_at is None


async def test_deleting_the_last_owner_ends_it_and_audits_owner_missing(session_factory, clock):
    org, people = await seed_org(session_factory)
    owner = people["owner"]
    await h.process_user_registered(session_factory, registered(user_id=str(owner), email="boss@amity.edu"), clock)

    await h.process_user_deleted(session_factory, deleted(owner), clock)             # does not block

    async with session_factory() as s:
        ended = (await s.execute(select(Membership).where(Membership.user_id == owner))).scalar_one()
        assert ended.status == "removed" and ended.end_reason == "user_deleted"
    audits = {a.action: a for a in await all_rows(session_factory, AuditLog)}
    assert set(audits) == {"member.removed", "org.owner_missing"}
    missing = audits["org.owner_missing"]
    assert (missing.org_id, missing.actor_kind, missing.target_id) == (org.id, "system", str(org.id))
    assert missing.details["cause"] == "user_deleted"
    assert len(await all_rows(session_factory, OutboxEvent)) == 1


async def test_deleting_an_owner_when_another_owner_remains_is_not_missing(session_factory, clock):
    org, people = await seed_org(session_factory, roles=("owner", "owner", "member"))
    async with session_factory() as s:
        owners = list((await s.execute(select(Membership).where(Membership.role == "owner"))).scalars())
    await h.process_user_deleted(session_factory, deleted(owners[0].user_id), clock)
    assert "org.owner_missing" not in [a.action for a in await all_rows(session_factory, AuditLog)]


async def test_a_user_with_no_membership_is_simply_marked_deleted(session_factory, clock):
    await h.process_user_registered(session_factory, registered(), clock)
    await h.process_user_deleted(session_factory, deleted(USER), clock)
    assert (await known(session_factory)).deleted_at == NOW
    assert await all_rows(session_factory, OutboxEvent) == [] and await all_rows(session_factory, AuditLog) == []


async def test_an_unknown_user_gets_a_placeholder_row_so_they_cannot_come_back(session_factory, clock):
    stranger = uuid.uuid4()
    await h.process_user_deleted(session_factory, deleted(stranger), clock)
    row = await known(session_factory, stranger)
    assert row.deleted_at == NOW and row.email == f"{stranger}@deleted.invalid"
    await h.process_user_registered(session_factory, registered(user_id=str(stranger), event_id="late-1"), clock)
    assert (await known(session_factory, stranger)).email == f"{stranger}@deleted.invalid"     # the late event is ignored


# ---------------------------------------------------------------- dedupe and atomicity
async def test_a_duplicate_event_id_is_processed_once(session_factory, clock):
    org, people = await seed_org(session_factory)
    member = people["member"]
    event = deleted(member)
    assert await h.process_user_deleted(session_factory, event, clock) is True
    assert await h.process_user_deleted(session_factory, event, clock) is False
    assert len(await all_rows(session_factory, OutboxEvent)) == 1 and len(await all_rows(session_factory, AuditLog)) == 1
    assert [(p.consumer, p.event_id) for p in await all_rows(session_factory, ProcessedEvent)] == [
        ("user_deleted", event.event_id)]

    first = registered(event_id="same-id", full_name="First")
    assert await h.process_user_registered(session_factory, first, clock) is True
    assert await h.process_user_registered(session_factory, registered(event_id="same-id", full_name="Second"),
                                           clock) is False
    assert (await known(session_factory)).full_name == "First"


async def test_the_same_event_id_for_different_consumers_is_independent(session_factory, clock):
    assert await h.process_user_registered(session_factory, registered(event_id="shared"), clock) is True
    assert await h.process_email_verified(session_factory, verified(event_id="shared"), clock) is True
    assert await h.record_ignored(session_factory, clock, "shared") is True
    assert await h.record_ignored(session_factory, clock, "shared") is False


async def test_the_dedupe_row_and_the_effects_commit_or_roll_back_together(session_factory, clock, monkeypatch):
    async def explode(session, event, now):
        await KnownUserRepositoryStub(session).upsert(event.user_id, "x@amity.edu", "X", now)
        raise RuntimeError("handler failed after writing")

    from app.repositories.known_user_repository import KnownUserRepository as KnownUserRepositoryStub
    monkeypatch.setattr(h, "handle_user_registered", explode)
    with pytest.raises(RuntimeError):
        await h.process_user_registered(session_factory, registered(), clock)
    assert await all_rows(session_factory, ProcessedEvent) == [] and await known(session_factory) is None
    monkeypatch.undo()
    assert await h.process_user_registered(session_factory, registered(), clock) is True    # a retry now succeeds


async def test_the_membership_repository_still_sees_nobody_active_after_deletion(session_factory, clock):
    org, people = await seed_org(session_factory)
    await h.process_user_deleted(session_factory, deleted(people["member"]), clock)
    async with session_factory() as s:
        assert await MembershipRepository(s).get_active_for_user(people["member"]) is None
        assert await MembershipRepository(s).count_active(org.id) == 1
