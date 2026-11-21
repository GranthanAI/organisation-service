"""Seats, sourced from staff (Phase 5). `SEAT_SOURCE=billing` is reserved for Phase 10: staff cannot set seats then.

`set_from_staff` writes `seat_state` (source `staff`, status `active`), moves a pilot or expired pilot to `active` when
a paid plan is set, and audits `seats.set` with the old and new values. It runs in the caller's transaction.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, plan_features
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import OrgStatus, OrgType, PlanCode, SeatSource, SeatStatus
from app.core.exceptions import NotFound, OrgInactive, SeatsManagedByBilling, ValidationFailed
from app.models.seat_state import SeatState
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import CurrentUser
from app.services import audit_service, org_lifecycle_rules


def snapshot(seat: SeatState | None) -> dict | None:
    if seat is None:
        return None
    return {"plan_code": seat.plan_code, "variant": seat.variant, "seats_bought": seat.seats_bought,
            "status": seat.status, "period_end": seat.period_end.isoformat() if seat.period_end else None,
            "source": seat.source}


def resolve_seats(plan: PlanCode, variant: OrgType, seats_bought: int | None) -> int | None:
    """The seat count that will be stored. pilot forces PILOT_MAX_MEMBERS, payg forces no cap, the paid plans need
    at least their per-variant minimum (ValidationFailed names it)."""
    if plan is PlanCode.PILOT:
        return settings.PILOT_MAX_MEMBERS
    if plan is PlanCode.PAYG:
        return None
    minimum = plan_features.min_seats(plan, variant)
    if seats_bought is None or seats_bought < 1:
        raise ValidationFailed(f"seats_bought is required for {plan.value}: at least {minimum} for a {variant.value}")
    if seats_bought < minimum:
        raise ValidationFailed(f"{plan.value} needs at least {minimum} seats for a {variant.value}")
    return seats_bought


async def set_from_staff(
    session: AsyncSession,
    org_id: uuid.UUID,
    plan_code: str,
    variant: str,
    seats_bought: int | None,
    period_end: datetime | None,
    actor: CurrentUser,
    reason: str,
    *,
    clock: Clock = system_clock,
) -> SeatState:
    if settings.SEAT_SOURCE != SeatSource.STAFF.value:
        raise SeatsManagedByBilling("seats come from billing")
    try:
        plan, variant_enum = PlanCode(plan_code), OrgType(variant)
    except ValueError as exc:
        raise ValidationFailed(f"unknown plan_code or variant: {exc}") from exc

    org = await OrgRepository(session).get_for_update(org_id)
    if org is None:
        raise NotFound("Organisation not found")
    if org.type != variant_enum.value:
        raise ValidationFailed(f"variant must equal the organisation's type ({org.type})")
    if org.status == OrgStatus.CLOSED.value:
        raise OrgInactive("a closed organisation cannot be given seats")
    seats = resolve_seats(plan, variant_enum, seats_bought)

    seat_repo = SeatStateRepository(session)
    seat = await seat_repo.lock_for_update(org_id)
    old = snapshot(seat)
    now = clock.now()
    if seat is None:
        seat = await seat_repo.add(SeatState(
            org_id=org_id, plan_code=plan.value, variant=variant_enum.value, seats_bought=seats,
            status=SeatStatus.ACTIVE.value, period_end=period_end, source=SeatSource.STAFF.value, refreshed_at=now))
    else:
        seat.plan_code, seat.variant, seat.seats_bought = plan.value, variant_enum.value, seats
        seat.status, seat.period_end, seat.source = SeatStatus.ACTIVE.value, period_end, SeatSource.STAFF.value
        seat.source_ref, seat.refreshed_at = None, now
        await seat_repo.save(seat)

    active = await MembershipRepository(session).count_active(org_id)
    over_by = max(0, active - seats) if seats is not None else 0       # nobody is removed; joins just stop
    await audit_service.record(
        session, actor, constants.AUDIT_SEATS_SET, "org", org_id,
        {"old": old, "new": snapshot(seat), "active_members": active, "over_by": over_by},
        reason, org_id=org_id, staff=True, clock=clock)

    # a paid plan on a pilot (or an expired pilot) makes it a normal, active organisation
    if plan in plan_features.PAID_PLANS and org.status in (OrgStatus.PILOT.value, OrgStatus.PILOT_EXPIRED.value):
        target = OrgStatus.ACTIVE
        org_lifecycle_rules.check_transition(OrgStatus(org.status), target)
        previous = org.status
        org.status, org.updated_at = target.value, now
        await OrgRepository(session).save(org)
        await audit_service.record(
            session, actor, constants.AUDIT_ORG_STATUS_CHANGED, "org", org_id,
            {"from": previous, "to": target.value, "cause": f"paid plan {plan.value} set"}, reason, org_id=org_id,
            staff=True, clock=clock)
    return seat

