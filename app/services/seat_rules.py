"""Can one more person join? Pure: stdlib and app/core only.

Every active membership is a seat, owners and admins included (D3). No overage: a full org refuses (D2).
`seats_bought=None` means no cap (Pay as you Go). Pilots are also capped at PILOT_MAX_MEMBERS.
"""

from enum import Enum

from app.core.config import settings
from app.core.enums import OrgStatus

# Only these states accept new members; pilot_expired, suspended and closed do not.
JOINABLE = frozenset({OrgStatus.PILOT, OrgStatus.ACTIVE})


class JoinDecision(str, Enum):
    OK = "ok"
    SEATS_FULL = "seats_full"
    PILOT_FULL = "pilot_full"
    ORG_INACTIVE = "org_inactive"


def can_join(
    org_status: OrgStatus,
    seats_bought: int | None,
    active_count: int,
    is_pilot: bool,
    pilot_max_members: int | None = None,
) -> JoinDecision:
    if org_status not in JOINABLE:
        return JoinDecision.ORG_INACTIVE
    if is_pilot:
        cap = settings.PILOT_MAX_MEMBERS if pilot_max_members is None else pilot_max_members
        if active_count >= cap:
            return JoinDecision.PILOT_FULL
    if seats_bought is not None and active_count >= seats_bought:
        return JoinDecision.SEATS_FULL
    return JoinDecision.OK
