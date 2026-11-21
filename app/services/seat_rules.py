"""Can one more person join? Pure: stdlib and app/core only.

Every active membership is a seat, owners and admins included (D3). No overage: a full org refuses (D2).
`seats_bought=None` means no cap (Pay as you Go). Pilots are also capped at `pilot_max_members`, which callers pass
from settings.PILOT_MAX_MEMBERS (this file reads no configuration).
"""

from app.core.enums import JoinDecision, OrgStatus

# Only these states accept new members; pilot_expired, suspended and closed do not.
JOINABLE = frozenset({OrgStatus.PILOT, OrgStatus.ACTIVE})


def can_join(
    org_status: OrgStatus,
    seats_bought: int | None,
    active_count: int,
    is_pilot: bool,
    *,
    pilot_max_members: int,
) -> JoinDecision:
    if org_status not in JOINABLE:
        return JoinDecision.ORG_INACTIVE
    if is_pilot:
        if active_count >= pilot_max_members:
            return JoinDecision.PILOT_FULL
    if seats_bought is not None and active_count >= seats_bought:
        return JoinDecision.SEATS_FULL
    return JoinDecision.OK
