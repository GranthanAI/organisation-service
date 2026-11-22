"""The organisation usage report (Phase 10): billing knows the numbers, this service adds names, emails and roles.

Dates are IST days, as billing uses them. At most 92 days (inclusive), `to` not before `from`, `from` not more than two years back.
billing only reports CURRENT members (see PHASES.md "Changes needed in other repos"), so someone who left mid-period is
missing from the numbers; a user in the numbers who has left since shows role null.
"""

import asyncio
import csv
import io
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.billing_client import BillingClient, get_billing_client
from app.core.clock import Clock, system_clock
from app.core.exceptions import FeatureNotInPlan, NotFound, ValidationFailed
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services.me_service import features_for
from app.utils.csv_safe import safe_cell

IST = timezone(timedelta(hours=5, minutes=30))
MAX_SPAN_DAYS = 92
MAX_AGE_DAYS = 730
DELETED_NAME = "Deleted user"


@dataclass
class MemberUsage:
    user_id: uuid.UUID
    name: str | None
    email: str | None
    role: str | None
    messages: int = 0
    billable_units: int = 0
    by_model_group: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class UsageReport:
    start: date
    end: date
    messages: int
    billable_units: int
    by_model_group: dict[str, dict[str, int]]
    members: list[MemberUsage]


def ist_today(clock: Clock) -> date:
    return clock.now().astimezone(IST).date()


def check_dates(start: date, end: date, clock: Clock) -> None:
    if end < start:
        raise ValidationFailed("'to' is before 'from'")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise ValidationFailed(f"a report covers at most {MAX_SPAN_DAYS} days")
    if start < ist_today(clock) - timedelta(days=MAX_AGE_DAYS):
        raise ValidationFailed("'from' is more than two years ago")


async def usage_report(session: AsyncSession, org_id: uuid.UUID, start: date, end: date, clock: Clock = system_clock,
                       billing: BillingClient | None = None) -> UsageReport:
    check_dates(start, end, clock)
    seat = await SeatStateRepository(session).get(org_id)
    if not features_for(seat.plan_code if seat else None)["usage_reports"]:
        raise FeatureNotInPlan("usage_reports is not included in this plan")
    body = await (billing or get_billing_client()).usage(org_id, start, end)      # UpstreamUnavailable (503) on any failure

    per_user: dict[uuid.UUID, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for row in body["rows"]:
        cell = per_user[uuid.UUID(str(row["user_id"]))][str(row["model_group"])]
        cell[0] += int(row.get("messages") or 0)
        cell[1] += int(row.get("billable_units") or 0)

    known = await KnownUserRepository(session).get_many(list(per_user))
    roles = {m.user_id: m.role for m in await MembershipRepository(session).list_active_for_org(org_id)}
    members: list[MemberUsage] = []
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for user_id, groups in per_user.items():
        person = known.get(user_id)
        deleted = person is not None and person.deleted_at is not None
        item = MemberUsage(user_id=user_id, name=DELETED_NAME if deleted else (person.full_name if person else None),
                           email=None if deleted or person is None else str(person.email), role=roles.get(user_id))
        for group, (messages, units) in sorted(groups.items()):
            item.by_model_group[group] = {"messages": messages, "billable_units": units}
            item.messages += messages
            item.billable_units += units
            totals[group][0] += messages
            totals[group][1] += units
        members.append(item)
    members.sort(key=lambda m: (-m.messages, str(m.user_id)))
    return UsageReport(
        start, end, sum(m.messages for m in members), sum(m.billable_units for m in members),
        {g: {"messages": v[0], "billable_units": v[1]} for g, v in sorted(totals.items())}, members)


def render_csv(report: UsageReport) -> bytes:
    """One row per member per model group; every cell through safe_cell (no formula runs when it is opened); a UTF-8 BOM so
    Excel reads it as UTF-8. CPU only: callers run it in a thread."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["user_id", "name", "email", "role", "model_group", "messages", "billable_units"])
    for member in report.members:
        for group, numbers in member.by_model_group.items():
            writer.writerow([safe_cell(c) for c in (member.user_id, member.name, member.email, member.role, group,
                                                    numbers["messages"], numbers["billable_units"])])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


async def render_csv_in_thread(report: UsageReport) -> bytes:
    return await asyncio.to_thread(render_csv, report)


async def org_slug(session: AsyncSession, org_id: uuid.UUID) -> str:
    org = await OrgRepository(session).get(org_id)
    if org is None:
        raise NotFound("Organisation not found")
    return org.slug


def filename(org_slug: str, start: date, end: date) -> str:
    return f"usage-{org_slug}-{start.isoformat()}-{end.isoformat()}.csv"

