"""GET /org/v1/me: the caller's organisation, role, seats and what their plan includes. One query for
membership + organisation + seat_state, and one count."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import plan_features
from app.core.enums import MembershipStatus, PlanCode, SeatStatus
from app.core.exceptions import NotFound
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.seat_state import SeatState

NO_SEATS = {SeatStatus.INCOMPLETE.value, SeatStatus.ENDED.value}
ACTIVE = MembershipStatus.ACTIVE.value


def features_for(plan_code: str | None) -> dict[str, bool | int | None]:
    """What the plan includes. An organisation with no seat row has no plan, so nothing paid is switched on."""
    if plan_code is None:
        return {"lock_modes": False, "shared_modes_max": 0, "usage_reports": False, "spend_cap": False,
                "csv_invites": True, "invite_links": True, "domains": True}
    f = plan_features.features(PlanCode(plan_code))
    return {"lock_modes": f.lock_modes, "shared_modes_max": f.shared_modes_max, "usage_reports": f.usage_reports,
            "spend_cap": f.member_spend_cap, "csv_invites": f.csv_invites, "invite_links": f.invite_links,
            "domains": f.domains}


async def get_me(session: AsyncSession, user_id: uuid.UUID) -> dict:
    row = (await session.execute(
        select(Membership.role, Organisation, SeatState)
        .join(Organisation, Organisation.id == Membership.org_id)
        .outerjoin(SeatState, SeatState.org_id == Membership.org_id)
        .where(Membership.user_id == user_id, Membership.status == ACTIVE))).first()
    if row is None:
        raise NotFound("You are not in an organisation")
    role, org, seat = row
    used = (await session.execute(select(func.count()).select_from(Membership).where(
        Membership.org_id == org.id, Membership.status == ACTIVE))).scalar_one()
    bought = None if seat is None or seat.status in NO_SEATS else seat.seats_bought
    return {
        "org": {"id": org.id, "name": org.name, "slug": org.slug, "type": org.type, "status": org.status,
                "pilot_ends_at": org.pilot_ends_at},
        "role": role,
        "seats": {"plan_code": seat.plan_code if seat else None, "status": seat.status if seat else None,
                  "used": used, "bought": bought, "over_by": max(0, used - bought) if bought is not None else 0},
        "features": features_for(seat.plan_code if seat else None),
    }
