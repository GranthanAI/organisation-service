import uuid

import pytest

from app.core.enums import StaffRole
from app.core.exceptions import Unauthenticated
from app.security.models import CurrentUser, staff_role_from_roles


@pytest.mark.parametrize("roles, expected", [
    ([], None),
    (["billing_admin"], None),                       # billing_admin alone is not staff here
    (["support"], StaffRole.SUPPORT),
    (["admin"], StaffRole.ADMIN),
    (["admin", "billing_admin", "support"], StaffRole.ADMIN),   # what auth-service issues for admin
    (["support", "billing_admin"], StaffRole.SUPPORT),
    (["billing_admin", "admin"], StaffRole.ADMIN),
    (["user"], None),
    (["ADMIN"], None),                               # exact match only
])
def test_staff_role_from_roles(roles, expected):
    assert staff_role_from_roles(roles) is expected


def claims(**kw):
    return {"sub": str(uuid.uuid4()), "email": "  Ada@Amity.EDU ", "roles": [], "jti": "j", **kw}


def test_current_user_from_claims_normalises_and_sets_the_staff_role():
    user = CurrentUser.from_claims(claims(roles=["admin", "support"]))
    assert user.email == "ada@amity.edu" and user.staff_role is StaffRole.ADMIN and isinstance(user.id, uuid.UUID)


def test_a_missing_roles_claim_means_no_roles_and_a_string_role_is_wrapped():
    assert CurrentUser.from_claims({**claims(), "roles": None}).roles == []
    assert CurrentUser.from_claims(claims(roles="support")).staff_role is StaffRole.SUPPORT


@pytest.mark.parametrize("bad", [
    {"sub": "not-a-uuid"}, {"email": "nope"}, {"roles": [1, 2]}, {"roles": {"a": 1}}, {"jti": ""}, {"jti": 5},
])
def test_malformed_claims_are_unauthenticated(bad):
    with pytest.raises(Unauthenticated):
        CurrentUser.from_claims(claims(**bad))


def test_the_jti_is_not_in_the_repr():
    assert "jti" not in repr(CurrentUser.from_claims(claims(jti="secret-jti-value")))
    assert "secret-jti-value" not in repr(CurrentUser.from_claims(claims(jti="secret-jti-value")))
