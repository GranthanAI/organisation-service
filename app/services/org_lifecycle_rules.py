"""OrgStatus transitions. Pure: stdlib and app/core only. `closed` is final."""

from datetime import datetime

from app.core.enums import OrgStatus
from app.core.exceptions import IllegalTransition

S = OrgStatus

TRANSITIONS: dict[OrgStatus, frozenset[OrgStatus]] = {
    S.PILOT: frozenset({S.ACTIVE, S.PILOT_EXPIRED, S.SUSPENDED, S.CLOSED}),
    # PILOT: staff extend an expired pilot
    S.PILOT_EXPIRED: frozenset({S.PILOT, S.ACTIVE, S.CLOSED}),
    S.ACTIVE: frozenset({S.SUSPENDED, S.CLOSED}),
    # PILOT / PILOT_EXPIRED: reactivating a suspended pilot (see reactivation_target)
    S.SUSPENDED: frozenset({S.PILOT, S.PILOT_EXPIRED, S.ACTIVE, S.CLOSED}),
    S.CLOSED: frozenset(),
}


def can_transition(current: OrgStatus, target: OrgStatus) -> bool:
    return target in TRANSITIONS[current]


def check_transition(current: OrgStatus, target: OrgStatus) -> None:
    """Raise IllegalTransition unless current -> target is in the table (staying in the same state is not a move)."""
    if not can_transition(current, target):
        raise IllegalTransition(f"cannot move an organisation from {current.value} to {target.value}")


def reactivation_target(was_pilot: bool, pilot_ends_at: datetime | None, now: datetime) -> OrgStatus:
    """Where a suspended organisation goes on reactivation: back to PILOT while its pilot is still running,
    PILOT_EXPIRED if the pilot ended while it was suspended, otherwise ACTIVE. `now` comes from the Clock."""
    if not was_pilot:
        return S.ACTIVE
    if pilot_ends_at is not None and pilot_ends_at > now:
        return S.PILOT
    return S.PILOT_EXPIRED
