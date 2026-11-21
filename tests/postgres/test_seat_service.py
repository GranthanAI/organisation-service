"""seat_service.set_from_staff: validation, minimums, payg, pilot cap, pilot to active, over_by, SEAT_SOURCE."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import FixedClock
from app.core.config import settings
from app.core.enums import OrgType, PlanCode
from app.core.exceptions import NotFound, OrgInactive, SeatsManagedByBilling, ValidationFailed
from app.core.plan_features import PLANS, min_seats
from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.organisation import Organisation
from app.models.seat_state import SeatState
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser
from app.services import seat_service
from tests.support.factories import NOW, make_membership, make_org, make_seat_state

STAFF = CurrentUser(id=uuid.UUID(int=99), email="staff@granthan.com", jti="j", roles=["admin"])
REASON = "sold by sales"


@pytest.fixture
def clock():
    return FixedClock(NOW)


async def seed(session_factory, status="active", type="college", members=0, with_seat=True, **seat):
    org = make_org(status=status, type=type, pilot_ends_at=NOW + timedelta(days=10) if status == "pilot" else None)
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        if with_seat:
            await SeatStateRepository(s).add(make_seat_state(org.id, variant=type, **seat))
        for _ in range(members):
            await MembershipRepository(s).add(make_membership(org.id))
    return org


async def put(session_factory, clock, org, plan, variant, seats, period_end=None):
    async with unit_of_work(session_factory) as s:
        return await seat_service.set_from_staff(s, org.id, plan, variant, seats, period_end, STAFF, REASON, clock=clock)


async def seat(session_factory, org) -> SeatState:
    async with session_factory() as s:
        return await s.get(SeatState, org.id)


async def status_of(session_factory, org) -> str:
    async with session_factory() as s:
        return (await s.get(Organisation, org.id)).status


async def audits(session_factory, action=None):
    async with session_factory() as s:
        rows = list((await s.execute(select(AuditLog).order_by(AuditLog.at, AuditLog.id))).scalars())
    return [r for r in rows if action is None or r.action == action]


def test_the_plan_table_and_minimums():
    assert set(PLANS) == set(PlanCode)
    assert [min_seats(PlanCode.ORG_PRO, v) for v in OrgType] == [50, 5]       # college, company
    assert [min_seats(PlanCode.ORG_MAX, v) for v in OrgType] == [10, 5]
    assert min_seats(PlanCode.PAYG, OrgType.COLLEGE) is None and min_seats(PlanCode.PILOT, OrgType.COMPANY) is None
    assert PLANS[PlanCode.PILOT].usage_reports is False and PLANS[PlanCode.PAYG].member_spend_cap is True
    assert PLANS[PlanCode.PAYG].shared_modes_max is None and PLANS[PlanCode.ORG_PRO].shared_modes_max == 20


# --- validation ---
@pytest.mark.parametrize("plan, variant", [("gold", "college"), ("org_pro", "school"), ("", "college")])
async def test_unknown_plan_or_variant_is_a_validation_error(session_factory, clock, plan, variant):
    org = await seed(session_factory)
    with pytest.raises(ValidationFailed):
        await put(session_factory, clock, org, plan, variant, 60)


async def test_variant_must_equal_the_orgs_type(session_factory, clock):
    org = await seed(session_factory, type="college")
    with pytest.raises(ValidationFailed, match="college"):
        await put(session_factory, clock, org, "org_pro", "company", 10)
    assert (await seat(session_factory, org)).plan_code == "org_pro" and await audits(session_factory) == []


@pytest.mark.parametrize("plan, type, seats, ok", [
    ("org_pro", "college", 49, False), ("org_pro", "college", 50, True), ("org_pro", "college", 51, True),
    ("org_pro", "company", 4, False), ("org_pro", "company", 5, True),
    ("org_max", "college", 9, False), ("org_max", "college", 10, True),
    ("org_max", "company", 4, False), ("org_max", "company", 5, True),
])
async def test_minimum_seats_per_plan_and_variant(session_factory, clock, plan, type, seats, ok):
    org = await seed(session_factory, type=type)
    if ok:
        assert (await put(session_factory, clock, org, plan, type, seats)).seats_bought == seats
    else:
        minimum = {"org_pro": {"college": 50, "company": 5}, "org_max": {"college": 10, "company": 5}}[plan][type]
        with pytest.raises(ValidationFailed, match=str(minimum)):
            await put(session_factory, clock, org, plan, type, seats)


@pytest.mark.parametrize("seats", [None, 0, -3])
async def test_a_paid_by_seat_plan_needs_a_seat_count(session_factory, clock, seats):
    org = await seed(session_factory)
    with pytest.raises(ValidationFailed, match="seats_bought is required"):
        await put(session_factory, clock, org, "org_pro", "college", seats)


async def test_payg_forces_no_cap(session_factory, clock):
    org = await seed(session_factory, type="company")
    result = await put(session_factory, clock, org, "payg", "company", 12)         # the number is ignored
    assert result.seats_bought is None and result.plan_code == "payg"
    assert (await seat(session_factory, org)).seats_bought is None


async def test_pilot_forces_the_pilot_cap(session_factory, clock):
    org = await seed(session_factory, status="pilot", plan_code="org_pro", seats_bought=60)
    result = await put(session_factory, clock, org, "pilot", "college", 3)
    assert result.seats_bought == settings.PILOT_MAX_MEMBERS == 25


async def test_status_and_source_are_set(session_factory, clock):
    org = await seed(session_factory)
    end = NOW + timedelta(days=365)
    row = await put(session_factory, clock, org, "org_max", "college", 20, end)
    assert (row.status, row.source, row.source_ref, row.refreshed_at, row.period_end) == ("active", "staff", None, NOW, end)


async def test_a_missing_seat_state_row_is_created(session_factory, clock):
    org = await seed(session_factory, with_seat=False)
    row = await put(session_factory, clock, org, "org_pro", "college", 60)
    assert row.seats_bought == 60 and (await seat(session_factory, org)) is not None


async def test_an_unknown_org_is_not_found_and_a_closed_org_is_refused(session_factory, clock):
    with pytest.raises(NotFound):
        async with unit_of_work(session_factory) as s:
            await seat_service.set_from_staff(s, uuid.uuid4(), "payg", "college", None, None, STAFF, REASON, clock=clock)
    closed = await seed(session_factory, status="closed")
    with pytest.raises(OrgInactive):
        await put(session_factory, clock, closed, "payg", "college", None)


# --- pilot to active ---
@pytest.mark.parametrize("start", ["pilot", "pilot_expired"])
@pytest.mark.parametrize("plan, seats", [("org_pro", 60), ("org_max", 10), ("payg", None)])
async def test_a_paid_plan_moves_a_pilot_to_active(session_factory, clock, start, plan, seats):
    org = await seed(session_factory, status=start, plan_code="pilot", seats_bought=25)
    await put(session_factory, clock, org, plan, "college", seats)
    assert await status_of(session_factory, org) == "active"
    (change,) = await audits(session_factory, "org.status_changed")
    assert change.details["from"] == start and change.details["to"] == "active" and change.actor_kind == "staff"


@pytest.mark.parametrize("status", ["active", "suspended"])
async def test_a_paid_plan_leaves_other_statuses_alone(session_factory, clock, status):
    org = await seed(session_factory, status=status)
    await put(session_factory, clock, org, "org_pro", "college", 60)
    assert await status_of(session_factory, org) == status and await audits(session_factory, "org.status_changed") == []


async def test_setting_the_pilot_plan_again_does_not_change_the_status(session_factory, clock):
    org = await seed(session_factory, status="pilot", plan_code="pilot", seats_bought=25)
    await put(session_factory, clock, org, "pilot", "college", None)
    assert await status_of(session_factory, org) == "pilot"


# --- over_by and the audit ---
async def test_seats_below_the_active_count_are_allowed_nobody_is_removed_and_over_by_is_audited(session_factory, clock):
    company = await seed(session_factory, type="company", members=8)
    await put(session_factory, clock, company, "org_max", "company", 5)
    async with session_factory() as s:
        assert await MembershipRepository(s).count_active(company.id) == 8        # nobody removed
    (row,) = await audits(session_factory, "seats.set")
    assert row.details["over_by"] == 3 and row.details["active_members"] == 8
    assert row.details["new"]["seats_bought"] == 5


async def test_within_the_seats_over_by_is_zero(session_factory, clock):
    org = await seed(session_factory, members=7)
    await put(session_factory, clock, org, "org_max", "college", 10)
    assert (await audits(session_factory, "seats.set"))[0].details["over_by"] == 0


async def test_the_audit_has_old_and_new_values_reason_and_actor(session_factory, clock):
    org = await seed(session_factory, plan_code="pilot", seats_bought=25, status="active")
    end = NOW + timedelta(days=365)
    await put(session_factory, clock, org, "org_pro", "college", 60, end)
    (row,) = await audits(session_factory, "seats.set")
    assert row.org_id == org.id and row.reason == REASON and row.actor_id == STAFF.id and row.actor_kind == "staff"
    assert row.details["old"]["plan_code"] == "pilot" and row.details["old"]["seats_bought"] == 25
    assert row.details["new"] == {"plan_code": "org_pro", "variant": "college", "seats_bought": 60, "status": "active",
                                  "period_end": end.isoformat(), "source": "staff"}


async def test_the_first_seat_state_has_no_old_values(session_factory, clock):
    org = await seed(session_factory, with_seat=False)
    await put(session_factory, clock, org, "payg", "college", None)
    assert (await audits(session_factory, "seats.set"))[0].details["old"] is None


# --- SEAT_SOURCE ---
async def test_seat_source_billing_refuses_with_409(session_factory, clock, monkeypatch):
    org = await seed(session_factory)
    monkeypatch.setattr(settings, "SEAT_SOURCE", "billing")
    with pytest.raises(SeatsManagedByBilling, match="seats come from billing") as exc:
        await put(session_factory, clock, org, "org_pro", "college", 60)
    assert exc.value.code == "SEATS_MANAGED_BY_BILLING"
    assert (await seat(session_factory, org)).seats_bought == 50 and await audits(session_factory) == []


async def test_a_failure_rolls_everything_back(session_factory, clock):
    org = await seed(session_factory, status="pilot", plan_code="pilot", seats_bought=25)
    with pytest.raises(RuntimeError):
        async with unit_of_work(session_factory) as s:
            await seat_service.set_from_staff(s, org.id, "org_pro", "college", 60, None, STAFF, REASON, clock=clock)
            raise RuntimeError("later step failed")
    assert (await seat(session_factory, org)).plan_code == "pilot" and await status_of(session_factory, org) == "pilot"
    assert await audits(session_factory) == []
