"""join_service.join: every refusal, owner rules, invite bookkeeping, audit, the joined event, and rollback."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.enums import JoinSource, Role
from app.core.exceptions import (
    AlreadyInOrg,
    ForbiddenRole,
    NotFound,
    OrgInactive,
    PilotFull,
    SeatsFull,
)
from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.invite import Invite
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.invite_repository import InviteRepository
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser
from app.services import join_service
from tests.support.factories import NOW, make_invite, make_membership, make_org, make_seat_state


@pytest.fixture
def clock():
    return FixedClock(NOW)


async def seed(session_factory, status="active", type="college", seats=5, seat_status="active", members=0,
               with_seat=True, owners=0, **org_kw):
    org = make_org(status=status, type=type, **org_kw)
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        if with_seat:
            await SeatStateRepository(s).add(make_seat_state(org.id, variant=type, seats_bought=seats,
                                                             status=seat_status))
        for _ in range(owners):
            await MembershipRepository(s).add(make_membership(org.id, role="owner"))
        for _ in range(members):
            await MembershipRepository(s).add(make_membership(org.id))
    return org


async def do_join(session_factory, clock, org, user=None, source=JoinSource.STAFF, role=Role.MEMBER, invite=None,
                  actor="system", staff=False):
    user = user or uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        m = await join_service.join(s, org.id, user, source, role, invite, actor=actor, staff=staff, clock=clock)
    return user, m


async def rows(session_factory, model):
    async with session_factory() as s:
        return list((await s.execute(select(model))).scalars())


# ---------------------------------------------------------------- the happy path
async def test_a_join_creates_an_active_membership_with_audit_and_event(session_factory, clock):
    org = await seed(session_factory, type="company")
    actor = CurrentUser(id=uuid.uuid4(), email="admin@x.co", jti="j", roles=["admin"])
    user, m = await do_join(session_factory, clock, org, source=JoinSource.STAFF, role=Role.ADMIN, actor=actor, staff=True)

    (row,) = [r for r in await rows(session_factory, Membership) if r.user_id == user]
    assert (row.org_id, row.role, row.status, row.source, row.joined_at) == (org.id, "admin", "active", "staff", NOW)
    assert row.ended_at is None and row.end_reason is None and row.invited_by is None and row.id == m.id

    (audit,) = await rows(session_factory, AuditLog)
    assert audit.action == "member.joined" and audit.org_id == org.id and audit.actor_kind == "staff"
    assert audit.actor_id == actor.id and audit.target_id == str(row.id)
    assert audit.details == {"user_id": str(user), "source": "staff", "role": "admin", "invite_id": None}

    (event,) = await rows(session_factory, OutboxEvent)
    assert event.topic == "org.member.joined" and event.key == str(user)           # keyed by user_id
    p = event.payload
    assert (p["org_id"], p["user_id"], p["role"], p["source"]) == (str(org.id), str(user), "admin", "staff")
    assert p["org_type"] == "company" and p["schema_version"] == "1.0" and p["occurred_at"] == "2026-11-21T12:00:00Z"


@pytest.mark.parametrize("source", list(JoinSource))
async def test_every_source_is_recorded(session_factory, clock, source):
    org = await seed(session_factory)
    _, m = await do_join(session_factory, clock, org, source=source)
    assert m.source == source.value
    assert (await rows(session_factory, OutboxEvent))[0].payload["source"] == source.value


# ---------------------------------------------------------------- refusals
@pytest.mark.parametrize("status", ["pilot_expired", "suspended", "closed"])
async def test_an_inactive_org_refuses_with_org_inactive(session_factory, clock, status):
    org = await seed(session_factory, status=status)
    with pytest.raises(OrgInactive):
        await do_join(session_factory, clock, org)
    assert await rows(session_factory, Membership) == [] and await rows(session_factory, OutboxEvent) == []


async def test_a_full_org_refuses_with_seats_full_and_one_seat_less_does_not(session_factory, clock):
    org = await seed(session_factory, seats=3, members=2)
    await do_join(session_factory, clock, org)                       # the third seat
    with pytest.raises(SeatsFull):
        await do_join(session_factory, clock, org)                   # no fourth
    assert len(await rows(session_factory, Membership)) == 3


async def test_a_pilot_is_full_at_25_members_with_pilot_full(session_factory, clock):
    org = await seed(session_factory, status="pilot", seats=25, members=24, pilot_ends_at=NOW + timedelta(days=5))
    await do_join(session_factory, clock, org)                       # the 25th
    with pytest.raises(PilotFull):
        await do_join(session_factory, clock, org)                   # the 26th
    assert len(await rows(session_factory, Membership)) == 25


async def test_a_pilot_is_capped_even_if_its_seat_row_has_no_cap(session_factory, clock):
    org = await seed(session_factory, status="pilot", seats=None, members=25, pilot_ends_at=NOW + timedelta(days=5))
    with pytest.raises(PilotFull):
        await do_join(session_factory, clock, org)


async def test_no_cap_means_no_limit_on_a_payg_org(session_factory, clock):
    org = await seed(session_factory, seats=None, members=30)
    await do_join(session_factory, clock, org)


@pytest.mark.parametrize("seat_status", ["incomplete", "ended"])
async def test_an_org_whose_seats_are_not_set_up_refuses_with_seats_full(session_factory, clock, seat_status):
    org = await seed(session_factory, seats=None, seat_status=seat_status, owners=1)   # NULL would otherwise read as "no cap"
    with pytest.raises(SeatsFull):
        await do_join(session_factory, clock, org)


async def test_an_org_with_no_seat_row_refuses_members_too(session_factory, clock):
    org = await seed(session_factory, with_seat=False, owners=1)
    with pytest.raises(SeatsFull):
        await do_join(session_factory, clock, org)


@pytest.mark.parametrize("seat_status", ["active", "past_due", "cancelling", "suspended"])
async def test_other_subscription_statuses_still_use_the_seat_count(session_factory, clock, seat_status):
    org = await seed(session_factory, seats=2, seat_status=seat_status, members=1)
    await do_join(session_factory, clock, org)
    with pytest.raises(SeatsFull):
        await do_join(session_factory, clock, org)


async def test_an_unknown_org_is_not_found(session_factory, clock):
    with pytest.raises(NotFound):
        async with unit_of_work(session_factory) as s:
            await join_service.join(s, uuid.uuid4(), uuid.uuid4(), JoinSource.STAFF, actor="system", clock=clock)


async def test_a_deleted_user_cannot_join(session_factory, clock):
    org = await seed(session_factory)
    gone = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).mark_deleted(gone, NOW)
    with pytest.raises(NotFound):
        await do_join(session_factory, clock, org, user=gone)
    assert await rows(session_factory, Membership) == []


# ---------------------------------------------------------------- already in an org
async def test_a_user_in_another_org_gets_already_in_org(session_factory, clock):
    a, b = await seed(session_factory), await seed(session_factory)
    user, _ = await do_join(session_factory, clock, a)
    with pytest.raises(AlreadyInOrg):
        await do_join(session_factory, clock, b, user=user)
    with pytest.raises(AlreadyInOrg):
        await do_join(session_factory, clock, a, user=user)                      # the same org twice


async def test_after_leaving_the_user_can_join_another_org(session_factory, clock):
    from app.core.enums import EndReason
    from app.services import membership_service
    a, b = await seed(session_factory), await seed(session_factory)
    user, _ = await do_join(session_factory, clock, a)
    async with unit_of_work(session_factory) as s:
        await membership_service.end(s, a.id, user, EndReason.LEFT, "system", clock=clock)
    await do_join(session_factory, clock, b, user=user)


async def test_the_unique_index_still_maps_to_already_in_org_when_the_pre_check_is_beaten(session_factory, clock,
                                                                                         monkeypatch):
    """A race the pre-check cannot see: only ux_memberships_one_active_org stops the second insert."""
    a, b = await seed(session_factory), await seed(session_factory)
    user, _ = await do_join(session_factory, clock, a)

    async def blind(self, user_id):
        return None

    monkeypatch.setattr(MembershipRepository, "get_active_for_user", blind)
    # 1) through unit_of_work
    with pytest.raises(AlreadyInOrg):
        await do_join(session_factory, clock, b, user=user)
    # 2) when the caller's own session flushes (no unit_of_work around it)
    async with session_factory() as s:
        with pytest.raises(AlreadyInOrg) as exc:
            await join_service.join(s, b.id, user, JoinSource.STAFF, actor="system", clock=clock)
        assert exc.value.code == "ALREADY_IN_ORG"
        await s.rollback()
    assert len(await rows(session_factory, Membership)) == 1
    assert len(await rows(session_factory, OutboxEvent)) == 1                  # only the first join's event


# ---------------------------------------------------------------- owners
async def test_the_first_owner_joins_even_before_seats_are_bought(session_factory, clock):
    org = await seed(session_factory, seats=None, seat_status="incomplete")
    user, m = await do_join(session_factory, clock, org, role=Role.OWNER)
    assert m.role == "owner"
    with pytest.raises(SeatsFull):                                               # but a member cannot
        await do_join(session_factory, clock, org)


async def test_the_first_owner_still_cannot_join_an_inactive_org(session_factory, clock):
    org = await seed(session_factory, status="suspended", seats=None, seat_status="incomplete")
    with pytest.raises(OrgInactive):
        await do_join(session_factory, clock, org, role=Role.OWNER)


async def test_a_second_owner_needs_staff(session_factory, clock):
    org = await seed(session_factory, owners=1)
    with pytest.raises(ForbiddenRole):
        await do_join(session_factory, clock, org, role=Role.OWNER, actor="system")
    staff = CurrentUser(id=uuid.uuid4(), email="s@g.com", jti="j", roles=["admin"])
    _, m = await do_join(session_factory, clock, org, role=Role.OWNER, actor=staff, staff=True)
    assert m.role == "owner"


async def test_admins_and_members_are_never_blocked_by_the_owner_rule(session_factory, clock):
    org = await seed(session_factory, owners=1)
    for role in (Role.ADMIN, Role.MEMBER):
        await do_join(session_factory, clock, org, role=role)


async def test_an_org_with_only_ended_owners_counts_as_ownerless(session_factory, clock):
    from app.core.enums import EndReason
    from app.services import membership_service
    org = await seed(session_factory, seats=10, owners=2)
    async with session_factory() as s:
        owners = [m.user_id for m in (await s.execute(select(Membership))).scalars()]
    for owner in owners:
        async with unit_of_work(session_factory) as s:
            await membership_service.end(s, org.id, owner, EndReason.USER_DELETED, "system", clock=clock)
    await do_join(session_factory, clock, org, role=Role.OWNER)                 # no active owner: allowed


# ---------------------------------------------------------------- invites
async def test_an_email_invite_is_marked_used_and_accepted(session_factory, clock):
    org = await seed(session_factory)
    creator = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        invite = await InviteRepository(s).add(make_invite(org.id, created_by=creator))
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        invite = await InviteRepository(s).get(invite.id)
        await join_service.join(s, org.id, user, JoinSource.INVITE, invite=invite, actor="system", clock=clock)
    async with session_factory() as s:
        got = await s.get(Invite, invite.id)
        (member,) = [m for m in (await s.execute(select(Membership))).scalars() if m.user_id == user]
    assert (got.uses, got.accepted_at, got.accepted_by) == (1, NOW, user)
    assert member.invited_by == creator
    (audit,) = await rows(session_factory, AuditLog)
    assert audit.details["invite_id"] == str(invite.id) and audit.details["source"] == "invite"


async def test_a_csv_invite_is_accepted_like_an_email_one(session_factory, clock):
    org = await seed(session_factory)
    async with unit_of_work(session_factory) as s:
        invite = await InviteRepository(s).add(make_invite(org.id, kind="csv", token_hash="csv-x"))
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        inv = await InviteRepository(s).get(invite.id)
        await join_service.join(s, org.id, user, JoinSource.CSV, invite=inv, actor="system", clock=clock)
    async with session_factory() as s:
        got = await s.get(Invite, invite.id)
    assert (got.uses, got.accepted_at, got.accepted_by) == (1, NOW, user)


async def test_a_link_invite_counts_uses_but_is_never_accepted(session_factory, clock):
    org = await seed(session_factory, seats=10)
    async with unit_of_work(session_factory) as s:
        link = await InviteRepository(s).add(make_invite(org.id, kind="link", email=None, max_uses=5, token_hash="l-1"))
    for _ in range(3):
        async with unit_of_work(session_factory) as s:
            inv = await InviteRepository(s).get(link.id)
            await join_service.join(s, org.id, uuid.uuid4(), JoinSource.LINK, invite=inv, actor="system", clock=clock)
    async with session_factory() as s:
        got = await s.get(Invite, link.id)
    assert got.uses == 3 and got.accepted_at is None and got.accepted_by is None


async def test_a_refused_join_does_not_use_the_invite(session_factory, clock):
    org = await seed(session_factory, seats=1, members=1)
    async with unit_of_work(session_factory) as s:
        invite = await InviteRepository(s).add(make_invite(org.id))
    with pytest.raises(SeatsFull):
        async with unit_of_work(session_factory) as s:
            inv = await InviteRepository(s).get(invite.id)
            await join_service.join(s, org.id, uuid.uuid4(), JoinSource.INVITE, invite=inv, actor="system", clock=clock)
    async with session_factory() as s:
        got = await s.get(Invite, invite.id)
    assert got.uses == 0 and got.accepted_at is None                       # the invite stays open


async def test_an_invite_from_another_org_is_a_programming_error(session_factory, clock):
    a, b = await seed(session_factory), await seed(session_factory)
    async with unit_of_work(session_factory) as s:
        invite = await InviteRepository(s).add(make_invite(b.id))
    with pytest.raises(ValueError):
        async with unit_of_work(session_factory) as s:
            inv = await InviteRepository(s).get(invite.id)
            await join_service.join(s, a.id, uuid.uuid4(), JoinSource.INVITE, invite=inv, actor="system", clock=clock)


# ---------------------------------------------------------------- rollback
async def test_everything_rolls_back_with_the_callers_transaction(session_factory, clock):
    org = await seed(session_factory)
    with pytest.raises(RuntimeError):
        async with unit_of_work(session_factory) as s:
            await join_service.join(s, org.id, uuid.uuid4(), JoinSource.STAFF, actor="system", clock=clock)
            await s.flush()
            raise RuntimeError("a later step failed")
    assert await rows(session_factory, Membership) == []
    assert await rows(session_factory, AuditLog) == [] and await rows(session_factory, OutboxEvent) == []
