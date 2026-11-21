import pytest

from app.core import exceptions as ex
from app.core.enums import JoinDecision
from app.services.seat_rules import can_join  # noqa: F401  (JoinDecision is re-exported from app.core.enums)


@pytest.mark.parametrize("cls, code", [
    (ex.AlreadyInOrg, "ALREADY_IN_ORG"),
    (ex.InviteAlreadyOpen, "INVITE_ALREADY_OPEN"),
    (ex.DomainTaken, "DOMAIN_TAKEN"),
    (ex.SeatsFull, "SEATS_FULL"),
    (ex.PilotFull, "PILOT_FULL"),
    (ex.OrgInactive, "ORG_INACTIVE"),
    (ex.InviteInvalid, "INVITE_INVALID"),
    (ex.InviteExpired, "INVITE_EXPIRED"),
    (ex.NotFound, "NOT_FOUND"),
    (ex.ForbiddenRole, "FORBIDDEN_ROLE"),
    (ex.LastOwner, "LAST_OWNER"),
])
def test_error_codes(cls, code):
    err = cls("why")
    assert err.code == code and isinstance(err, ex.DomainError) and err.detail == "why"


@pytest.mark.parametrize("decision, expected", [
    (JoinDecision.OK, None),
    (JoinDecision.SEATS_FULL, ex.SeatsFull),
    (JoinDecision.PILOT_FULL, ex.PilotFull),
    (JoinDecision.ORG_INACTIVE, ex.OrgInactive),
])
def test_join_decision_error(decision, expected):
    err = ex.join_decision_error(decision)
    if expected is None:
        assert err is None
    else:
        assert type(err) is expected


def test_every_join_decision_is_mapped():
    for decision in JoinDecision:
        ex.join_decision_error(decision)  # no KeyError
