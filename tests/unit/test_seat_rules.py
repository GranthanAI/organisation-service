import inspect

import pytest

from app.core.enums import OrgStatus
from app.services.seat_rules import JoinDecision
from app.services.seat_rules import can_join as _can_join

PILOT_MAX = 25


def can_join(status, seats, active, is_pilot, pilot_max_members=PILOT_MAX):
    return _can_join(status, seats, active, is_pilot, pilot_max_members=pilot_max_members)


OK, FULL, PILOT_FULL, INACTIVE = (JoinDecision.OK, JoinDecision.SEATS_FULL, JoinDecision.PILOT_FULL,
                                  JoinDecision.ORG_INACTIVE)


@pytest.mark.parametrize("active, expected", [(49, OK), (50, FULL), (51, FULL)])
def test_boundary_seats_minus_one_seats_seats_plus_one(active, expected):
    # seats = 50: with 49 active one seat is free; with 50 or 51 (over-sold) the org is full.
    assert can_join(OrgStatus.ACTIVE, 50, active, is_pilot=False) is expected


def test_zero_seats_is_full():
    assert can_join(OrgStatus.ACTIVE, 0, 0, is_pilot=False) is FULL


@pytest.mark.parametrize("active", [0, 1, 1_000, 1_000_000])
def test_none_seats_means_no_cap(active):
    assert can_join(OrgStatus.ACTIVE, None, active, is_pilot=False) is OK


@pytest.mark.parametrize("active, expected", [(24, OK), (25, PILOT_FULL), (26, PILOT_FULL)])
def test_pilot_member_25_vs_26(active, expected):
    # A pilot holds 25 members: the 25th may join (24 active), the 26th may not (25 active).
    assert can_join(OrgStatus.PILOT, 25, active, is_pilot=True) is expected


def test_pilot_cap_applies_even_when_seats_are_none():
    assert can_join(OrgStatus.PILOT, None, 25, is_pilot=True) is PILOT_FULL
    assert can_join(OrgStatus.PILOT, None, 24, is_pilot=True) is OK


def test_pilot_cap_is_checked_before_seats():
    assert can_join(OrgStatus.PILOT, 25, 25, is_pilot=True) is PILOT_FULL


def test_pilot_with_fewer_seats_than_cap_is_seats_full():
    assert can_join(OrgStatus.PILOT, 10, 10, is_pilot=True) is FULL


def test_pilot_cap_can_be_overridden():
    assert can_join(OrgStatus.PILOT, None, 5, is_pilot=True, pilot_max_members=5) is PILOT_FULL
    assert can_join(OrgStatus.PILOT, None, 4, is_pilot=True, pilot_max_members=5) is OK


def test_non_pilot_ignores_pilot_cap():
    assert can_join(OrgStatus.ACTIVE, 500, 100, is_pilot=False) is OK


@pytest.mark.parametrize("status", [OrgStatus.PILOT_EXPIRED, OrgStatus.SUSPENDED, OrgStatus.CLOSED])
@pytest.mark.parametrize("seats", [None, 0, 100])
def test_inactive_orgs_refuse_before_counting_seats(status, seats):
    assert can_join(status, seats, 0, is_pilot=False) is INACTIVE
    assert can_join(status, seats, 0, is_pilot=True) is INACTIVE


@pytest.mark.parametrize("status", [OrgStatus.PILOT, OrgStatus.ACTIVE])
def test_joinable_states(status):
    assert can_join(status, None, 0, is_pilot=False) is OK


def test_pilot_max_members_is_a_required_keyword():
    param = inspect.signature(_can_join).parameters["pilot_max_members"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        _can_join(OrgStatus.PILOT, None, 0, True)  # type: ignore[call-arg]


def test_payment_due_refuses_new_joins_whatever_the_count():
    assert _can_join(OrgStatus.ACTIVE, 50, 0, False, pilot_max_members=PILOT_MAX, payment_due=True) is JoinDecision.PAYMENT_DUE
    assert _can_join(OrgStatus.ACTIVE, None, 3, False, pilot_max_members=PILOT_MAX, payment_due=True) is JoinDecision.PAYMENT_DUE
    assert _can_join(OrgStatus.ACTIVE, 50, 0, False, pilot_max_members=PILOT_MAX, payment_due=False) is OK


def test_an_inactive_org_wins_over_payment_due():
    assert _can_join(OrgStatus.SUSPENDED, 50, 0, False, pilot_max_members=PILOT_MAX, payment_due=True) is INACTIVE


def test_payment_due_statuses_are_past_due_and_suspended():
    from app.services.seat_rules import refuses_new_joins
    assert refuses_new_joins("past_due") and refuses_new_joins("suspended")
    for status in ("active", "cancelling", "incomplete", "ended", None):
        assert not refuses_new_joins(status), status
