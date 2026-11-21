"""OrgStatus transitions. Pure: stdlib and app/core only. `closed` is final."""

from app.core.enums import OrgStatus
from app.core.exceptions import IllegalTransition

S = OrgStatus

TRANSITIONS: dict[OrgStatus, frozenset[OrgStatus]] = {
    S.PILOT: frozenset({S.ACTIVE, S.PILOT_EXPIRED, S.SUSPENDED, S.CLOSED}),
    S.PILOT_EXPIRED: frozenset({S.ACTIVE, S.CLOSED}),
    S.ACTIVE: frozenset({S.SUSPENDED, S.CLOSED}),
    S.SUSPENDED: frozenset({S.ACTIVE, S.CLOSED}),
    S.CLOSED: frozenset(),
}


def can_transition(current: OrgStatus, target: OrgStatus) -> bool:
    return target in TRANSITIONS[current]


def check_transition(current: OrgStatus, target: OrgStatus) -> None:
    """Raise IllegalTransition unless current -> target is in the table (staying in the same state is not a move)."""
    if not can_transition(current, target):
        raise IllegalTransition(f"cannot move an organisation from {current.value} to {target.value}")
