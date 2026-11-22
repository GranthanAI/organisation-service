"""Seats from billing (SEAT_SOURCE=billing): refresh, the 404 cases, subscription.changed, the nightly sweep, staff writes refused."""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from structlog.testing import capture_logs

from app.clients.billing_client import NoSubscription, RouteMissing, Subscription
from app.core.clock import FixedClock
from app.core.config import settings
from app.core.exceptions import SeatsManagedByBilling, UpstreamUnavailable
from app.db.session import unit_of_work
from app.events import topics
from app.events.schemas import SubscriptionChangedPayload
from app.models.audit_log import AuditLog
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.processed_event import ProcessedEvent
from app.models.seat_state import SeatState
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser
from app.services import seat_jobs, seat_service, seat_source
from app.services.seat_source import BillingSeatSource, StaffSeatSource, get_seat_source
from app.workers.consumer_worker import ConsumerWorker
from tests.support.factories import NOW, make_membership, make_org, make_seat_state

PERIOD_END = datetime(2027, 11, 1, tzinfo=timezone.utc)


class FakeBillingClient:
    """BillingClient's subscription(): answers per organisation from `answers`, or raises what is queued."""

    def __init__(self) -> None:
        self.answers: dict[uuid.UUID, Subscription | Exception] = {}
        self.default: Subscription | Exception | None = None
        self.calls: list[uuid.UUID] = []

    async def subscription(self, org_id):
        self.calls.append(org_id)
        answer = self.answers.get(org_id, self.default)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise NoSubscription()
        return answer


def sub(plan="org_pro", seats=60, status="active", variant="college", ref="sub-1", period_end=PERIOD_END) -> Subscription:
    return Subscription(plan_code=plan, variant=variant, seats=seats, status=status, period_end=period_end, subscription_id=ref)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def billing(monkeypatch):
    fake = FakeBillingClient()
    monkeypatch.setattr(settings, "SEAT_SOURCE", "billing")
    monkeypatch.setattr(seat_source, "get_billing_client", lambda: fake)
    seat_source.reset_warnings()
    return fake


async def seed(session_factory, *, status="active", seat=True, members=1, plan="pilot", seats=25, source="staff", type="college"):
    org = make_org(status=status, type=type)
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        if seat:
            await SeatStateRepository(s).add(make_seat_state(org.id, plan_code=plan, seats_bought=seats, source=source, variant=type))
        for i in range(members):
            await MembershipRepository(s).add(make_membership(org.id, uuid.uuid4(), role="owner" if i == 0 else "member"))
    return org


async def refresh(session_factory, org, clock):
    async with unit_of_work(session_factory) as s:
        return await get_seat_source().refresh(s, org.id, clock)


async def seat_of(session_factory, org) -> SeatState | None:
    async with session_factory() as s:
        return await s.get(SeatState, org.id)


async def audits(session_factory, action="seats.synced"):
    async with session_factory() as s:
        return [a for a in (await s.execute(select(AuditLog))).scalars() if a.action == action]


# ---------------------------------------------------------------- refresh
async def test_the_source_follows_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "SEAT_SOURCE", "staff")
    assert isinstance(get_seat_source(), StaffSeatSource)
    monkeypatch.setattr(settings, "SEAT_SOURCE", "billing")
    assert isinstance(get_seat_source(), BillingSeatSource)


async def test_refresh_creates_the_seat_row_from_billing(session_factory, clock, billing):
    org = await seed(session_factory, seat=False)
    billing.default = sub(plan="org_pro", seats=60, ref="sub-77")
    result = await refresh(session_factory, org, clock)
    assert (result.outcome, result.changed) == ("changed", True)
    seat = await seat_of(session_factory, org)
    assert (seat.plan_code, seat.variant, seat.seats_bought, seat.status, seat.source, seat.source_ref, seat.period_end, seat.refreshed_at) == (
        "org_pro", "college", 60, "active", "billing", "sub-77", PERIOD_END, NOW)
    (audit,) = await audits(session_factory)
    assert audit.actor_kind == "system" and audit.details["old"] is None and audit.details["new"]["seats_bought"] == 60
    assert billing.calls == [org.id]


async def test_refresh_updates_and_audits_only_when_values_changed(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = sub(seats=60, ref=None)
    async with unit_of_work(session_factory) as s:
        (await SeatStateRepository(s).get(org.id)).period_end = PERIOD_END           # already what billing will say
    first = await refresh(session_factory, org, clock)
    assert first.outcome == "unchanged" and await audits(session_factory) == []
    billing.default = sub(seats=80, ref=None)
    assert (await refresh(session_factory, org, clock)).outcome == "changed"
    (audit,) = await audits(session_factory)
    assert (audit.details["old"]["seats_bought"], audit.details["new"]["seats_bought"]) == (60, 80)
    assert (await refresh(session_factory, org, clock)).outcome == "unchanged"
    assert len(await audits(session_factory)) == 1


async def test_seats_dropping_below_members_removes_nobody_and_says_how_many_over(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing", members=5)
    billing.default = sub(seats=3)
    await refresh(session_factory, org, clock)
    assert (await seat_of(session_factory, org)).seats_bought == 3
    async with session_factory() as s:
        active = (await s.execute(select(func.count()).select_from(Membership).where(Membership.org_id == org.id, Membership.status == "active"))).scalar_one()
    assert active == 5                                                                       # nobody was removed
    (audit,) = await audits(session_factory)
    assert (audit.details["active_members"], audit.details["over_by"]) == (5, 2)


async def test_a_paid_plan_activates_a_pilot_and_payg_has_no_cap(session_factory, clock, billing):
    org = await seed(session_factory, status="pilot", plan="pilot", seats=25)
    billing.default = sub(plan="payg", seats=99)
    await refresh(session_factory, org, clock)
    seat = await seat_of(session_factory, org)
    assert (seat.plan_code, seat.seats_bought) == ("payg", None)
    async with session_factory() as s:
        assert (await s.get(Organisation, org.id)).status == "active"
    assert len(await audits(session_factory, "org.status_changed")) == 1


@pytest.mark.parametrize("billing_status, stored", [("active", "active"), ("past_due", "past_due"), ("cancelling", "cancelling"),
                                                    ("incomplete", "incomplete"), ("ended", "ended"), ("canceled", "ended"),
                                                    ("trialing", "active"), ("unpaid", "past_due")])
async def test_statuses(session_factory, clock, billing, billing_status, stored):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = sub(status=billing_status)
    await refresh(session_factory, org, clock)
    assert (await seat_of(session_factory, org)).status == stored


async def test_an_unknown_plan_or_status_leaves_the_state_alone(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    for bad in (sub(plan="platinum"), sub(status="on_fire")):
        billing.default = bad
        assert (await refresh(session_factory, org, clock)).outcome == "skipped"
    assert (await seat_of(session_factory, org)).seats_bought == 60 and await audits(session_factory) == []


# ---------------------------------------------------------------- the two 404s
async def test_a_404_for_the_organisation_means_no_subscription_and_the_seats_end(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = NoSubscription()
    result = await refresh(session_factory, org, clock)
    assert (result.outcome, result.changed) == ("ended", True)
    seat = await seat_of(session_factory, org)
    assert (seat.status, seat.source) == ("ended", "billing")
    assert len(await audits(session_factory)) == 1
    assert (await refresh(session_factory, org, clock)).outcome == "unchanged"                  # already ended
    bare = await seed(session_factory, seat=False)
    assert (await refresh(session_factory, bare, clock)).outcome == "no_subscription" and await seat_of(session_factory, bare) is None


async def test_a_404_for_the_route_logs_one_warning_per_process_and_changes_nothing(session_factory, clock, billing):
    first = await seed(session_factory, plan="org_pro", seats=60, source="staff")
    second = await seed(session_factory, plan="org_pro", seats=40, source="staff")
    billing.default = RouteMissing()
    with capture_logs() as logs:
        assert (await refresh(session_factory, first, clock)).outcome == "route_missing"
        assert (await refresh(session_factory, second, clock)).outcome == "route_missing"
        assert (await refresh(session_factory, first, clock)).outcome == "route_missing"
    warnings = [e for e in logs if e["log_level"] == "warning" and "route yet" in e["event"]]
    assert len(warnings) == 1
    assert [(await seat_of(session_factory, o)).seats_bought for o in (first, second)] == [60, 40]
    assert (await seat_of(session_factory, first)).source == "staff" and await audits(session_factory) == []


async def test_billing_down_propagates_so_the_caller_retries(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = UpstreamUnavailable("billing is down")
    with pytest.raises(UpstreamUnavailable):
        await refresh(session_factory, org, clock)
    assert (await seat_of(session_factory, org)).seats_bought == 60


# ---------------------------------------------------------------- subscription.changed
def changed(owner_type, owner, event_id=None, **kw) -> SubscriptionChangedPayload:
    return SubscriptionChangedPayload(event_id=event_id or str(uuid.uuid4()), owner_type=owner_type, owner_id=owner, **kw)


async def test_subscription_changed_for_a_user_is_ignored(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    assert await seat_source.process_subscription_changed(session_factory, changed("user", org.id), clock) is True
    assert billing.calls == []


async def test_subscription_changed_for_an_org_refreshes_and_is_deduplicated(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = sub(seats=75)
    event = changed("org", org.id, plan_code="org_pro", status="active")
    assert await seat_source.process_subscription_changed(session_factory, event, clock) is True
    assert await seat_source.process_subscription_changed(session_factory, event, clock) is False       # a duplicate
    assert billing.calls == [org.id] and (await seat_of(session_factory, org)).seats_bought == 75
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(ProcessedEvent).where(ProcessedEvent.consumer == "subscription_changed"))).scalar_one() == 1


async def test_the_event_is_only_a_hint_the_seats_come_from_the_refresh(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = sub(seats=61)
    await seat_source.process_subscription_changed(session_factory, changed("org", org.id, plan_code="org_max", status="past_due"), clock)
    seat = await seat_of(session_factory, org)
    assert (seat.plan_code, seat.seats_bought, seat.status) == ("org_pro", 61, "active")


async def test_a_failed_refresh_rolls_the_claim_back_so_the_event_is_tried_again(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = UpstreamUnavailable("down")
    event = changed("org", org.id)
    with pytest.raises(UpstreamUnavailable):
        await seat_source.process_subscription_changed(session_factory, event, clock)
    billing.default = sub(seats=70)
    assert await seat_source.process_subscription_changed(session_factory, event, clock) is True      # not a duplicate
    assert (await seat_of(session_factory, org)).seats_bought == 70


async def test_the_consumer_routes_the_topic_and_the_topic_is_subscribed_only_in_billing_mode(session_factory, clock, billing, monkeypatch):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = sub(seats=66)
    worker = ConsumerWorker(SimpleNamespace(), SimpleNamespace(), session_factory, clock)
    body = {"event_type": "SubscriptionChanged", "schema_version": "1.0", "event_id": str(uuid.uuid4()), "occurred_at": "2026-11-21T12:00:00Z",
            "billing_account_id": str(uuid.uuid4()), "owner_type": "org", "owner_id": str(org.id), "subscription_id": "sub-1",
            "plan_code": "org_pro", "status": "active", "current_period_end": "2027-11-01T00:00:00Z"}   # billing's SubscriptionChangedMsg
    await worker._dispatch(SimpleNamespace(topic="subscription.changed", value=json.dumps(body).encode(), partition=0, offset=1, key=b"k"))
    assert (await seat_of(session_factory, org)).seats_bought == 66
    assert topics.SUBSCRIPTION_CHANGED in topics.consumed_topics()
    monkeypatch.setattr(settings, "SEAT_SOURCE", "staff")
    assert topics.SUBSCRIPTION_CHANGED not in topics.consumed_topics() and topics.consumed_topics() == topics.CONSUMED_TOPICS


# ---------------------------------------------------------------- the nightly sweep
async def test_the_nightly_job_refreshes_in_batches_of_50_and_skips_pilots(session_factory, clock, billing):
    paid = [await seed(session_factory, plan="org_pro", seats=60, source="billing", members=1) for _ in range(120)]
    staff_paid = await seed(session_factory, plan="org_max", seats=60, source="staff", members=1)
    pilots = [await seed(session_factory, plan="pilot", seats=25, source="staff", members=1) for _ in range(5)]
    billing.default = sub(seats=75)
    totals = await seat_source.refresh_all(session_factory, clock)
    assert totals == {"changed": 121}
    assert set(billing.calls) == {o.id for o in paid} | {staff_paid.id} and not {p.id for p in pilots} & set(billing.calls)
    assert len(billing.calls) == 121 and len(set(billing.calls)) == 121                       # each exactly once
    assert (await seat_of(session_factory, paid[0])).seats_bought == 75
    assert seat_source.REFRESH_BATCH == 50


async def test_one_organisation_failing_does_not_stop_the_sweep(session_factory, clock, billing):
    orgs = [await seed(session_factory, plan="org_pro", seats=60, source="billing") for _ in range(4)]
    billing.default = sub(seats=75)
    billing.answers[orgs[1].id] = UpstreamUnavailable("down")
    totals = await seat_source.refresh_all(session_factory, clock)
    assert totals == {"changed": 3, "failed": 1}


async def test_the_job_handler_does_nothing_in_staff_mode_and_sweeps_in_billing_mode(session_factory, clock, billing, monkeypatch):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    billing.default = sub(seats=77)
    monkeypatch.setattr(settings, "SEAT_SOURCE", "staff")
    async with unit_of_work(session_factory) as s:
        await seat_jobs.seats_refresh_all(s, None, clock)
    assert billing.calls == []
    monkeypatch.setattr(settings, "SEAT_SOURCE", "billing")
    async with unit_of_work(session_factory) as s:
        await seat_jobs.seats_refresh_all(s, None, clock)
    assert (await seat_of(session_factory, org)).seats_bought == 77
    from app.services.job_handlers import RECURRING, REGISTRY
    assert "seats.refresh_all" in RECURRING and "seats.refresh_all" in REGISTRY


# ---------------------------------------------------------------- staff writes are refused in billing mode
async def test_staff_cannot_set_seats_when_billing_owns_them(session_factory, clock, billing):
    org = await seed(session_factory, plan="org_pro", seats=60, source="billing")
    staff = CurrentUser(id=uuid.uuid4(), email="staff@granthan.com", jti="j", roles=["admin"])
    with pytest.raises(SeatsManagedByBilling):
        async with unit_of_work(session_factory) as s:
            await seat_service.set_from_staff(s, org.id, "org_pro", "college", 90, None, staff, "trying to set seats by hand", clock=clock)
    assert (await seat_of(session_factory, org)).seats_bought == 60
