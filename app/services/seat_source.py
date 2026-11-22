"""Where seats come from (Phase 10): a `SeatSource` port with two implementations.

`StaffSeatSource` is today's: staff set seats (`seat_service.set_from_staff`), there is nothing to fetch. `BillingSeatSource`
(`SEAT_SOURCE=billing`) asks billing-service for the organisation's subscription (`GET /internal/orgs/{id}/subscription`) and
upserts `seat_state` with source `billing`. Staff writes are refused with SEATS_MANAGED_BY_BILLING in that mode.

`subscription.changed` is only a HINT: for an organisation (`owner_type == "org"`) it triggers `refresh()`; the nightly job
`seats.refresh_all` catches lost events. If billing has not built the route yet (a 404 for the route itself) the first
refresh logs one warning per process and every refresh leaves `seat_state` as it is. If seats drop below the active members,
nobody is removed: joins stop and /me shows `over_by`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.billing_client import BillingClient, NoSubscription, RouteMissing, Subscription, get_billing_client
from app.core import constants, plan_features
from app.core.clock import Clock, system_clock
from app.core.config import settings
from app.core.enums import OrgStatus, PlanCode, SeatSource, SeatStatus
from app.core.logging import logger
from app.db.session import unit_of_work
from app.events.schemas import SubscriptionChangedPayload
from app.models.seat_state import SeatState
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import audit_service, org_lifecycle_rules
from app.services.seat_service import snapshot

REFRESH_BATCH = 50
_STATUS_ALIASES = {"canceled": "ended", "cancelled": "ended", "expired": "ended", "trialing": "active", "unpaid": "past_due"}
_warned_route_missing = False


def reset_warnings() -> None:
    global _warned_route_missing
    _warned_route_missing = False


@dataclass(frozen=True)
class RefreshResult:
    outcome: str          # unchanged | changed | ended | route_missing | no_org | no_subscription | skipped
    changed: bool = False


class SeatSourcePort(Protocol):
    name: str

    async def refresh(self, session: AsyncSession, org_id: uuid.UUID, clock: Clock = system_clock) -> RefreshResult: ...


class StaffSeatSource:
    """Staff set seats by hand; there is nothing to refresh."""

    name = SeatSource.STAFF.value

    async def refresh(self, session: AsyncSession, org_id: uuid.UUID, clock: Clock = system_clock) -> RefreshResult:
        return RefreshResult("unchanged")


class BillingSeatSource:
    name = SeatSource.BILLING.value

    def __init__(self, client: BillingClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> BillingClient:
        return self._client or get_billing_client()

    async def refresh(self, session: AsyncSession, org_id: uuid.UUID, clock: Clock = system_clock) -> RefreshResult:
        global _warned_route_missing
        try:
            subscription = await self.client.subscription(org_id)       # UpstreamUnavailable propagates: the caller retries
        except RouteMissing:
            if not _warned_route_missing:
                _warned_route_missing = True
                logger.warning("billing-service has no /internal/orgs/{id}/subscription route yet: seats stay as they are")
            return RefreshResult("route_missing")
        except NoSubscription:
            subscription = None

        org = await OrgRepository(session).get_for_update(org_id)
        if org is None:
            return RefreshResult("no_org")
        seats = SeatStateRepository(session)
        seat = await seats.lock_for_update(org_id)
        now = clock.now()
        if subscription is None:
            if seat is None:
                return RefreshResult("no_subscription")
            old = snapshot(seat)
            seat.status, seat.source, seat.source_ref, seat.refreshed_at = SeatStatus.ENDED.value, SeatSource.BILLING.value, None, now
            return await self._finish(session, org, seat, old, now, clock, "ended")

        values = self._values(subscription, org.type)
        if values is None:
            return RefreshResult("skipped")
        old = snapshot(seat)
        if seat is None:
            seat = await seats.add(SeatState(org_id=org_id, refreshed_at=now, source=SeatSource.BILLING.value, **values))
        else:
            for key, value in values.items():
                setattr(seat, key, value)
            seat.source, seat.refreshed_at = SeatSource.BILLING.value, now
        return await self._finish(session, org, seat, old, now, clock, "unchanged")

    @staticmethod
    def _values(sub: Subscription, org_type: str) -> dict | None:
        try:
            plan = PlanCode(sub.plan_code)
        except ValueError:
            logger.error("billing sent a plan this service does not know", plan_code=sub.plan_code)
            return None
        status = _STATUS_ALIASES.get(sub.status, sub.status)
        if status not in {s.value for s in SeatStatus}:
            logger.error("billing sent a subscription status this service does not know", status=sub.status)
            return None
        return {"plan_code": plan.value, "variant": sub.variant or org_type, "seats_bought": None if plan is PlanCode.PAYG else sub.seats,
                "status": status, "period_end": sub.period_end, "source_ref": sub.subscription_id}

    @staticmethod
    async def _finish(session: AsyncSession, org, seat: SeatState, old: dict | None, now: datetime, clock: Clock,
                      ended_outcome: str) -> RefreshResult:
        await SeatStateRepository(session).save(seat)
        new = snapshot(seat)
        if new == old:
            return RefreshResult("unchanged")
        active = await MembershipRepository(session).count_active(org.id)
        over_by = max(0, active - seat.seats_bought) if seat.seats_bought is not None else 0
        await audit_service.record(session, "system", constants.AUDIT_SEATS_SYNCED, "org", org.id,
                                   {"old": old, "new": new, "active_members": active, "over_by": over_by},
                                   org_id=org.id, clock=clock)
        if PlanCode(seat.plan_code) in plan_features.PAID_PLANS and org.status in (OrgStatus.PILOT.value, OrgStatus.PILOT_EXPIRED.value) \
                and seat.status in (SeatStatus.ACTIVE.value, SeatStatus.CANCELLING.value):
            previous = org.status
            org_lifecycle_rules.check_transition(OrgStatus(previous), OrgStatus.ACTIVE)
            org.status, org.updated_at = OrgStatus.ACTIVE.value, now
            await OrgRepository(session).save(org)
            await audit_service.record(session, "system", constants.AUDIT_ORG_STATUS_CHANGED, "org", org.id,
                                       {"from": previous, "to": OrgStatus.ACTIVE.value, "cause": f"paid plan {seat.plan_code} from billing"},
                                       org_id=org.id, clock=clock)
        return RefreshResult(ended_outcome if ended_outcome == "ended" else "changed", True)


def get_seat_source() -> SeatSourcePort:
    return BillingSeatSource() if settings.SEAT_SOURCE == SeatSource.BILLING.value else StaffSeatSource()


# ---------------------------------------------------------------------------------------------------- the event and the job
async def handle_subscription_changed(session: AsyncSession, event: SubscriptionChangedPayload, clock: Clock) -> RefreshResult | None:
    """billing's subscription.changed: only organisations count, and the event is a hint, so refresh."""
    if event.owner_type != "org":
        return None
    return await get_seat_source().refresh(session, event.owner_id, clock)


async def refresh_all(factory: async_sessionmaker[AsyncSession], clock: Clock = system_clock) -> dict[str, int]:
    """The nightly sweep: every org with source billing or a plan other than pilot, REFRESH_BATCH at a time, one transaction
    per batch. One organisation failing (billing unreachable) is logged and skipped; the next night tries again."""
    totals: dict[str, int] = {}
    after: uuid.UUID | None = None
    source = get_seat_source()
    while True:
        async with unit_of_work(factory) as session:
            ids = await SeatStateRepository(session).refreshable_ids(after, REFRESH_BATCH)
        if not ids:
            break
        for org_id in ids:
            try:
                async with unit_of_work(factory) as session:
                    result = await source.refresh(session, org_id, clock)
            except Exception as exc:
                logger.warning("seat refresh failed for an organisation", org_id=str(org_id), error=type(exc).__name__)
                totals["failed"] = totals.get("failed", 0) + 1
                continue
            totals[result.outcome] = totals.get(result.outcome, 0) + 1
        after = ids[-1]
        if len(ids) < REFRESH_BATCH:
            break
    return totals


CONSUMER_SUBSCRIPTION_CHANGED = "subscription_changed"


async def process_subscription_changed(factory: async_sessionmaker[AsyncSession], event: SubscriptionChangedPayload,
                                       clock: Clock) -> bool:
    """Once per event_id (processed_events). If billing cannot be reached the transaction rolls back, the claim with it, and the
    consumer retries and finally dead-letters. True if it ran, False if it was a duplicate."""
    from app.services.user_event_handlers import process_once

    async def apply(session: AsyncSession) -> None:
        await handle_subscription_changed(session, event, clock)

    return await process_once(factory, clock, CONSUMER_SUBSCRIPTION_CHANGED, event.event_id, apply)
