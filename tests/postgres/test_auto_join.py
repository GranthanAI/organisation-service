"""Domain auto-join on EmailVerified: who joins, who is skipped, and what a full org does."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.config import settings
from app.core.enums import Role
from app.db.session import unit_of_work
from app.events.schemas import EmailVerifiedPayload, UserRegisteredPayload
from app.models.audit_log import AuditLog
from app.models.domain import JoinRequest, OrgDomain
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser, OrgContext
from app.services import domain_service
from app.services import user_event_handlers as h
from app.services.permission_rules import Action
from tests.support.factories import NOW, make_membership, make_org, make_seat_state


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture(autouse=True)
def auto_join_on(monkeypatch):
    monkeypatch.setattr(settings, "DOMAIN_AUTO_JOIN_ENABLED", True)


def stamp() -> datetime:
    return datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)


def registered(user, email, name="New Student") -> UserRegisteredPayload:
    return UserRegisteredPayload(event_id=str(uuid.uuid4()), timestamp=stamp(), user_id=user, email=email, full_name=name)


def verified(user, email) -> EmailVerifiedPayload:
    return EmailVerifiedPayload(event_id=str(uuid.uuid4()), timestamp=stamp(), user_id=user, email=email)


async def seed_org(session_factory, domain="amity.edu", *, status="active", seats=50, members=1, auto_join=True,
                   include_subdomains=False, domain_verified=True, name="Amity University"):
    org = make_org(name=name, status=status)
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, seats_bought=seats, plan_code="org_pro"))
        for i in range(members):
            user = uuid.uuid4()
            await KnownUserRepository(s).upsert(user, f"member{i}-{org.id.hex[:6]}@{domain}", f"Member {i}", NOW)
            await MembershipRepository(s).add(make_membership(org.id, user, role="owner" if i == 0 else "member"))
        if domain:
            s.add(OrgDomain(org_id=org.id, domain=domain, verify_token="t" * 43, verified_at=NOW if domain_verified else None,
                            verified_by=None, verify_method="dns" if domain_verified else None, auto_join=auto_join and domain_verified,
                            include_subdomains=include_subdomains, failed_checks=0, added_at=NOW))
    return org


async def register_and_verify(session_factory, clock, email, user=None, *, do_verify=True):
    user = user or uuid.uuid4()
    await h.process_user_registered(session_factory, registered(user, email), clock)
    if do_verify:
        await h.process_email_verified(session_factory, verified(user, email), clock)
    return user


async def table(session_factory, model):
    async with session_factory() as s:
        return list((await s.execute(select(model))).scalars())


async def active_membership(session_factory, user):
    async with session_factory() as s:
        return await MembershipRepository(s).get_active_for_user(user)


async def joined_events(session_factory, user):
    return [e for e in await table(session_factory, OutboxEvent) if e.topic == "org.member.joined" and e.key == str(user)]


# ---------------------------------------------------------------- joins
async def test_a_verified_domain_with_auto_join_and_a_free_seat_makes_a_member(session_factory, clock):
    org = await seed_org(session_factory)
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    m = await active_membership(session_factory, user)
    assert (m.org_id, m.role, m.source, m.status, m.invited_by) == (org.id, "member", "domain", "active", None)
    (event,) = await joined_events(session_factory, user)                                   # the PHASES exit check: exactly one
    assert event.payload["source"] == "domain" and event.payload["org_id"] == str(org.id) and event.payload["org_type"] == "college"
    joined = [a for a in await table(session_factory, AuditLog) if a.action == "member.joined" and a.details["user_id"] == str(user)]
    assert len(joined) == 1 and joined[0].actor_kind == "system" and joined[0].details["source"] == "domain"
    assert await table(session_factory, JoinRequest) == []


async def test_the_same_event_twice_joins_once(session_factory, clock):
    await seed_org(session_factory)
    user = uuid.uuid4()
    await h.process_user_registered(session_factory, registered(user, "x@amity.edu"), clock)
    event = verified(user, "x@amity.edu")
    assert await h.process_email_verified(session_factory, event, clock) is True
    assert await h.process_email_verified(session_factory, event, clock) is False            # deduplicated by processed_events
    assert len(await joined_events(session_factory, user)) == 1


async def test_a_subdomain_email_joins_only_with_include_subdomains(session_factory, clock):
    exact = await seed_org(session_factory, "amity.edu", include_subdomains=False, name="Exact")
    stranger = await register_and_verify(session_factory, clock, "x@cse.amity.edu")
    assert await active_membership(session_factory, stranger) is None
    async with unit_of_work(session_factory) as s:                                           # switch include_subdomains on
        row = await s.get(OrgDomain, (exact.id, "amity.edu"))
        row.include_subdomains = True
    later = await register_and_verify(session_factory, clock, "y@cse.student.amity.edu")
    assert (await active_membership(session_factory, later)).org_id == exact.id               # any depth
    still_out = await active_membership(session_factory, stranger)
    assert still_out is None                                                                  # no retroactive join for the earlier one


async def test_the_longest_match_wins(session_factory, clock):
    wide = await seed_org(session_factory, "amity.edu", include_subdomains=True, name="Wide")
    narrow = await seed_org(session_factory, "cse.amity.edu", include_subdomains=False, name="Narrow")
    a = await register_and_verify(session_factory, clock, "a@cse.amity.edu")
    b = await register_and_verify(session_factory, clock, "b@law.amity.edu")
    c = await register_and_verify(session_factory, clock, "c@amity.edu")
    assert (await active_membership(session_factory, a)).org_id == narrow.id                  # cse.amity.edu beats amity.edu
    assert (await active_membership(session_factory, b)).org_id == wide.id                    # only the parent covers law.
    assert (await active_membership(session_factory, c)).org_id == wide.id


async def test_a_full_org_makes_a_pending_request_and_a_second_event_adds_no_duplicate(session_factory, clock):
    org = await seed_org(session_factory, seats=1, members=1)                                  # 1 of 1
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    assert await active_membership(session_factory, user) is None
    (request,) = await table(session_factory, JoinRequest)
    assert (request.org_id, request.user_id, request.domain, request.status, request.decided_by) == (org.id, user, "amity.edu", "pending", None)
    created = [a for a in await table(session_factory, AuditLog) if a.action == "join_request.created"]
    assert len(created) == 1 and created[0].actor_kind == "system"
    await h.process_email_verified(session_factory, verified(user, "x@amity.edu"), clock)       # another EmailVerified (a new event id)
    assert len(await table(session_factory, JoinRequest)) == 1
    assert len([a for a in await table(session_factory, AuditLog) if a.action == "join_request.created"]) == 1


async def test_a_full_pilot_makes_a_pending_request_too(session_factory, clock, monkeypatch):
    monkeypatch.setattr(settings, "PILOT_MAX_MEMBERS", 2)
    org = await seed_org(session_factory, status="pilot", seats=25, members=2)
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    assert await active_membership(session_factory, user) is None
    assert [r.org_id for r in await table(session_factory, JoinRequest)] == [org.id]


# ---------------------------------------------------------------- nothing happens
async def test_flag_off_does_nothing(session_factory, clock, monkeypatch):
    monkeypatch.setattr(settings, "DOMAIN_AUTO_JOIN_ENABLED", False)
    await seed_org(session_factory)
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    assert await active_membership(session_factory, user) is None and await table(session_factory, JoinRequest) == []


async def test_auto_join_off_on_the_domain_does_nothing(session_factory, clock):
    await seed_org(session_factory, auto_join=False)
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    assert await active_membership(session_factory, user) is None and await table(session_factory, JoinRequest) == []


async def test_user_registered_alone_never_joins(session_factory, clock):
    await seed_org(session_factory)
    user = await register_and_verify(session_factory, clock, "x@amity.edu", do_verify=False)
    assert await active_membership(session_factory, user) is None and await table(session_factory, JoinRequest) == []
    async with session_factory() as s:
        assert (await KnownUserRepository(s).get(user)).email_verified_at is None


async def test_an_unverified_domain_does_nothing(session_factory, clock):
    await seed_org(session_factory, domain_verified=False)
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    assert await active_membership(session_factory, user) is None


async def test_a_user_already_in_an_org_is_skipped(session_factory, clock):
    amity = await seed_org(session_factory)
    other = await seed_org(session_factory, "other.edu", name="Other")
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).upsert(user, "x@amity.edu", "Busy", NOW)
        await MembershipRepository(s).add(make_membership(other.id, user, role="member"))
    await h.process_email_verified(session_factory, verified(user, "x@amity.edu"), clock)
    assert (await active_membership(session_factory, user)).org_id == other.id
    assert [m for m in await table(session_factory, Membership) if m.user_id == user and m.org_id == amity.id] == []
    assert await table(session_factory, JoinRequest) == []


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_an_inactive_org_is_skipped(session_factory, clock, status):
    await seed_org(session_factory, status=status)
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    assert await active_membership(session_factory, user) is None and await table(session_factory, JoinRequest) == []


async def test_a_deleted_user_is_skipped(session_factory, clock):
    await seed_org(session_factory)
    user = uuid.uuid4()
    await h.process_user_registered(session_factory, registered(user, "x@amity.edu"), clock)
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).mark_deleted(user, NOW)
    await h.process_email_verified(session_factory, verified(user, "x@amity.edu"), clock)
    assert await active_membership(session_factory, user) is None and await table(session_factory, JoinRequest) == []


async def test_an_email_that_is_not_in_any_verified_domain_does_nothing(session_factory, clock):
    await seed_org(session_factory)
    for email in ("x@gmail.com", "x@notamity.edu", "x@amity.edu.evil.com"):
        user = await register_and_verify(session_factory, clock, email)
        assert await active_membership(session_factory, user) is None


# ---------------------------------------------------------------- no retroactive mass join
async def test_verifying_a_domain_or_turning_auto_join_on_adds_nobody(session_factory, clock):
    org = await seed_org(session_factory, auto_join=False)
    early = [await register_and_verify(session_factory, clock, f"early{i}@amity.edu") for i in range(3)]      # verified BEFORE auto-join
    owner = CurrentUser(id=uuid.uuid4(), email="o@amity.edu", jti="j")
    ctx = OrgContext(org_id=org.id, user=owner, role=Role.OWNER, membership_id=uuid.uuid4(), action=Action.MANAGE_DOMAINS)
    async with unit_of_work(session_factory) as s:
        await domain_service.update_domain(s, ctx, "amity.edu", True, None, clock)             # auto_join on: nobody is pulled in
    for user in early:
        assert await active_membership(session_factory, user) is None
    assert await table(session_factory, JoinRequest) == []
    late = await register_and_verify(session_factory, clock, "late@amity.edu")                   # only a NEW EmailVerified joins
    assert (await active_membership(session_factory, late)).org_id == org.id


async def test_a_clock_in_the_future_does_not_matter(session_factory):
    clock = FixedClock(NOW + timedelta(days=400))
    org = await seed_org(session_factory)
    user = await register_and_verify(session_factory, clock, "x@amity.edu")
    assert (await active_membership(session_factory, user)).org_id == org.id
