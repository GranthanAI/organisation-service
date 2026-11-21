"""membership_service.end: every reason, the last-owner rule, owner_missing, event, audit, and the seat_state lock."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.clock import FixedClock
from app.core.enums import EndReason, Role
from app.core.exceptions import LastOwner, NotFound
from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser, ServiceCaller
from app.services import membership_service
from tests.support.factories import NOW, make_membership, make_org, make_seat_state


@pytest.fixture
def clock():
    return FixedClock(NOW)


async def seed(session_factory, roles=("owner", "admin", "member"), org_type="college", with_seats=True):
    org = make_org(type=org_type)
    users = {}
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        if with_seats:
            await SeatStateRepository(s).add(make_seat_state(org.id))
        for role in roles:
            users.setdefault(role, []).append(uuid.uuid4())
            await MembershipRepository(s).add(make_membership(org.id, users[role][-1], role=role))
    return org, users


async def end(session_factory, clock, org, user, reason, actor="system", **kw):
    async with unit_of_work(session_factory) as s:
        return await membership_service.end(s, org.id, user, reason, actor, clock=clock, **kw)


async def membership(session_factory, org, user) -> Membership:
    async with session_factory() as s:
        return (await s.execute(select(Membership).where(Membership.org_id == org.id,
                                                         Membership.user_id == user))).scalar_one()


async def table(session_factory, model):
    async with session_factory() as s:
        return list((await s.execute(select(model))).scalars())


@pytest.mark.parametrize("reason", list(EndReason))
async def test_end_for_each_reason(session_factory, clock, reason):
    org, users = await seed(session_factory)
    member = users["member"][0]
    result = await end(session_factory, clock, org, member, reason)
    assert result.owner_missing is False

    row = await membership(session_factory, org, member)
    assert row.status == ("left" if reason is EndReason.LEFT else "removed")
    assert row.ended_at == NOW and row.end_reason == reason.value

    (audit,) = await table(session_factory, AuditLog)
    assert audit.action == ("member.left" if reason is EndReason.LEFT else "member.removed")
    assert (audit.org_id, audit.target_type, audit.target_id) == (org.id, "membership", str(row.id))
    assert audit.actor_kind == "system" and audit.actor_id is None and audit.at == NOW
    assert audit.details == {"user_id": str(member), "role": "member", "reason": reason.value}

    (event,) = await table(session_factory, OutboxEvent)
    assert event.topic == "org.member.left" and event.key == str(member)       # keyed by user_id, not org_id
    assert event.published_at is None
    p = event.payload
    assert p["org_id"] == str(org.id) and p["user_id"] == str(member) and p["role"] == "member"
    assert p["org_type"] == "college" and p["reason"] == reason.value
    assert p["schema_version"] == "1.0" and p["occurred_at"] == "2026-11-21T12:00:00Z" and uuid.UUID(p["event_id"])


async def test_org_type_company_goes_out_on_the_event(session_factory, clock):
    org, users = await seed(session_factory, org_type="company")
    await end(session_factory, clock, org, users["member"][0], EndReason.LEFT)
    (event,) = await table(session_factory, OutboxEvent)
    assert event.payload["org_type"] == "company"


@pytest.mark.parametrize("reason", [EndReason.LEFT, EndReason.REMOVED])
async def test_the_last_owner_cannot_leave_or_be_removed_and_nothing_is_written(session_factory, clock, reason):
    org, users = await seed(session_factory)
    owner = users["owner"][0]
    with pytest.raises(LastOwner):
        await end(session_factory, clock, org, owner, reason)
    assert (await membership(session_factory, org, owner)).status == "active"
    assert await table(session_factory, OutboxEvent) == [] and await table(session_factory, AuditLog) == []


@pytest.mark.parametrize("reason", [EndReason.USER_DELETED, EndReason.ORG_CLOSED])
async def test_system_endings_remove_the_last_owner_and_report_owner_missing(session_factory, clock, reason):
    org, users = await seed(session_factory)
    owner = users["owner"][0]
    result = await end(session_factory, clock, org, owner, reason)
    assert result.owner_missing is True
    assert (await membership(session_factory, org, owner)).status == "removed"
    (event,) = await table(session_factory, OutboxEvent)
    assert event.payload["reason"] == reason.value and event.payload["role"] == "owner"


@pytest.mark.parametrize("reason", list(EndReason))
async def test_an_owner_can_end_when_another_owner_remains(session_factory, clock, reason):
    org, users = await seed(session_factory, roles=("owner", "owner", "member"))
    result = await end(session_factory, clock, org, users["owner"][0], reason)
    assert result.owner_missing is False


async def test_ending_a_non_member_or_an_ended_member_is_not_found(session_factory, clock):
    org, users = await seed(session_factory)
    with pytest.raises(NotFound):
        await end(session_factory, clock, org, uuid.uuid4(), EndReason.LEFT)
    member = users["member"][0]
    await end(session_factory, clock, org, member, EndReason.LEFT)
    with pytest.raises(NotFound):
        await end(session_factory, clock, org, member, EndReason.LEFT)
    assert len(await table(session_factory, OutboxEvent)) == 1


async def test_a_member_of_another_org_is_not_found(session_factory, clock):
    org, _ = await seed(session_factory)
    _other, other_users = await seed(session_factory)
    with pytest.raises(NotFound):
        await end(session_factory, clock, org, other_users["member"][0], EndReason.REMOVED)


async def test_the_actor_decides_the_audit_kind(session_factory, clock):
    org, users = await seed(session_factory, roles=("owner", "member", "member", "member"))
    staff = CurrentUser(id=uuid.uuid4(), email="s@granthan.com", jti="j", roles=["admin"])
    await end(session_factory, clock, org, users["member"][0], EndReason.REMOVED, staff, staff=True)
    await end(session_factory, clock, org, users["member"][1], EndReason.LEFT, CurrentUser(
        id=users["member"][1], email="m@amity.edu", jti="j"))
    await end(session_factory, clock, org, users["member"][2], EndReason.USER_DELETED, ServiceCaller("billing"))
    kinds = sorted((a.actor_kind, a.actor_id is None) for a in await table(session_factory, AuditLog))
    assert kinds == [("service", True), ("staff", False), ("user", False)]
    service_row = next(a for a in await table(session_factory, AuditLog) if a.actor_kind == "service")
    assert service_row.details["service"] == "billing"


async def test_end_locks_the_seat_state_row_until_the_transaction_ends(session_factory, clock):
    org, users = await seed(session_factory)
    async with unit_of_work(session_factory) as s1:
        await membership_service.end(s1, org.id, users["member"][0], EndReason.LEFT, "system", clock=clock)
        async with session_factory() as s2:      # a join in another connection would wait right here
            await s2.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                await SeatStateRepository(s2).lock_for_update(org.id)
    async with unit_of_work(session_factory) as s3:
        assert await SeatStateRepository(s3).lock_for_update(org.id) is not None   # free once s1 committed


async def test_an_org_without_a_seat_state_row_is_locked_through_the_organisation(session_factory, clock):
    org, users = await seed(session_factory, with_seats=False)
    async with unit_of_work(session_factory) as s1:
        await membership_service.end(s1, org.id, users["member"][0], EndReason.LEFT, "system", clock=clock)
        async with session_factory() as s2:
            await s2.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                await OrgRepository(s2).get_for_update(org.id)


async def test_the_whole_use_case_rolls_back_with_the_callers_transaction(session_factory, clock):
    org, users = await seed(session_factory)
    with pytest.raises(RuntimeError):
        async with unit_of_work(session_factory) as s:
            await membership_service.end(s, org.id, users["member"][0], EndReason.LEFT, "system", clock=clock)
            raise RuntimeError("a later step failed")
    assert (await membership(session_factory, org, users["member"][0])).status == "active"
    assert await table(session_factory, OutboxEvent) == [] and await table(session_factory, AuditLog) == []


def test_role_enum_is_what_the_event_carries():
    assert Role.OWNER.value == "owner"
