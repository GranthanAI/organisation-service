"""Every (from, to) pair of OrgStatus, allowed and forbidden."""

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import OrgStatus
from app.core.exceptions import IllegalTransition
from app.services.org_lifecycle_rules import can_transition, check_transition, reactivation_target

S = OrgStatus
ALLOWED = {
    (S.PILOT, S.ACTIVE), (S.PILOT, S.PILOT_EXPIRED), (S.PILOT, S.SUSPENDED), (S.PILOT, S.CLOSED),
    (S.PILOT_EXPIRED, S.PILOT), (S.PILOT_EXPIRED, S.ACTIVE), (S.PILOT_EXPIRED, S.CLOSED),
    (S.ACTIVE, S.SUSPENDED), (S.ACTIVE, S.CLOSED),
    (S.SUSPENDED, S.PILOT), (S.SUSPENDED, S.PILOT_EXPIRED), (S.SUSPENDED, S.ACTIVE), (S.SUSPENDED, S.CLOSED),
}
ALL_PAIRS = list(itertools.product(S, S))


def test_table_covers_every_pair():
    assert len(ALL_PAIRS) == 25
    assert len(ALLOWED) == 13


@pytest.mark.parametrize("current, target", ALL_PAIRS)
def test_every_pair(current, target):
    if (current, target) in ALLOWED:
        assert can_transition(current, target)
        check_transition(current, target)
    else:
        assert not can_transition(current, target)
        with pytest.raises(IllegalTransition):
            check_transition(current, target)


@pytest.mark.parametrize("target", list(S))
def test_closed_is_final(target):
    with pytest.raises(IllegalTransition):
        check_transition(S.CLOSED, target)


@pytest.mark.parametrize("status", list(S))
def test_staying_put_is_not_a_transition(status):
    with pytest.raises(IllegalTransition):
        check_transition(status, status)


def test_active_cannot_go_back_to_pilot():
    with pytest.raises(IllegalTransition):
        check_transition(S.ACTIVE, S.PILOT)


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("was_pilot, ends_at, expected", [
    (False, None, S.ACTIVE),
    (False, NOW + timedelta(days=5), S.ACTIVE),
    (False, NOW - timedelta(days=5), S.ACTIVE),
    (True, NOW + timedelta(seconds=1), S.PILOT),
    (True, NOW, S.PILOT_EXPIRED),            # ends exactly now: over
    (True, NOW - timedelta(seconds=1), S.PILOT_EXPIRED),
    (True, None, S.PILOT_EXPIRED),           # a pilot with no end date cannot be shown to be running
])
def test_reactivation_target(was_pilot, ends_at, expected):
    assert reactivation_target(was_pilot, ends_at, NOW) is expected


def test_every_reactivation_target_is_a_legal_move_from_suspended():
    for was_pilot, ends in [(False, None), (True, NOW + timedelta(days=1)), (True, NOW - timedelta(days=1))]:
        target = reactivation_target(was_pilot, ends, NOW)
        assert can_transition(S.SUSPENDED, target)
