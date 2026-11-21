"""Walks every (action, role) cell of the permission matrix (HLD "Roles and permissions")."""

import pytest

from app.core.enums import Role, StaffRole
from app.core.exceptions import ForbiddenRole
from app.services.permission_rules import ORG_TABLE, STAFF_TABLE, Action, Permission, check, permission_for

A, D, O = Permission.ALLOWED, Permission.DENIED, Permission.OWN_ROLE_ONLY

# action: owner, admin, member, staff support, staff admin
EXPECTED = {
    Action.VIEW_ORG:                   (A, A, A, A, A),
    Action.INVITE:                     (A, A, D, D, A),
    Action.REVOKE_INVITE:              (A, A, D, D, A),
    Action.CSV_UPLOAD:                 (A, A, D, D, A),
    Action.MANAGE_LINKS:               (A, A, D, D, A),
    Action.REMOVE_MEMBER:              (A, O, D, D, A),
    Action.CHANGE_ROLE:                (A, D, D, D, A),
    Action.TRANSFER_OWNERSHIP:         (A, D, D, D, A),
    Action.CLOSE_ORG:                  (A, D, D, D, A),
    Action.EDIT_DETAILS:               (A, A, D, D, A),
    Action.MANAGE_DOMAINS:             (A, A, D, D, A),
    Action.EDIT_POLICIES:              (A, A, D, D, A),
    Action.VIEW_USAGE:                 (A, A, D, A, A),
    Action.VIEW_AUDIT:                 (A, A, D, A, A),
    Action.LEAVE:                      (A, A, A, D, D),
    Action.STAFF_CREATE_ORG:           (D, D, D, D, A),
    Action.STAFF_SUSPEND:              (D, D, D, D, A),
    Action.STAFF_EXTEND_PILOT:         (D, D, D, D, A),
    Action.STAFF_MANUAL_DOMAIN_VERIFY: (D, D, D, D, A),
    Action.STAFF_SET_SEATS:            (D, D, D, D, A),
}
ACTORS = [Role.OWNER, Role.ADMIN, Role.MEMBER, StaffRole.SUPPORT, StaffRole.ADMIN]


def test_every_action_has_a_row_and_every_cell_is_present():
    assert set(EXPECTED) == set(Action)
    assert len(ORG_TABLE) == len(Action) * len(Role)
    assert len(STAFF_TABLE) == len(Action) * len(StaffRole)


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("actor_index", range(len(ACTORS)))
def test_every_cell_of_the_matrix(action, actor_index):
    actor = ACTORS[actor_index]
    expected = EXPECTED[action][actor_index]
    assert permission_for(action, actor) is expected
    if expected is A:
        check(action, actor)
    else:
        with pytest.raises(ForbiddenRole):
            check(action, actor)  # OwnRoleOnly fails closed without a target role


def test_org_admin_and_staff_admin_are_different_rows():
    # Role.ADMIN == StaffRole.ADMIN as strings, so the tables must be dispatched by class.
    assert permission_for(Action.STAFF_SET_SEATS, Role.ADMIN) is D
    assert permission_for(Action.STAFF_SET_SEATS, StaffRole.ADMIN) is A
    assert permission_for(Action.CHANGE_ROLE, Role.ADMIN) is D
    assert permission_for(Action.CHANGE_ROLE, StaffRole.ADMIN) is A


@pytest.mark.parametrize(
    "target, allowed",
    [(Role.MEMBER, True), (Role.ADMIN, False), (Role.OWNER, False)],
)
def test_admin_removes_members_only(target, allowed):
    if allowed:
        check(Action.REMOVE_MEMBER, Role.ADMIN, target)
    else:
        with pytest.raises(ForbiddenRole):
            check(Action.REMOVE_MEMBER, Role.ADMIN, target)


@pytest.mark.parametrize("target", list(Role))
def test_owner_and_staff_admin_may_remove_any_role(target):
    check(Action.REMOVE_MEMBER, Role.OWNER, target)
    check(Action.REMOVE_MEMBER, StaffRole.ADMIN, target)


def test_support_is_read_only():
    writes = [a for a in Action if permission_for(a, StaffRole.SUPPORT) is not A]
    reads = [a for a in Action if permission_for(a, StaffRole.SUPPORT) is A]
    assert set(reads) == {Action.VIEW_ORG, Action.VIEW_USAGE, Action.VIEW_AUDIT}
    assert Action.INVITE in writes


def test_a_non_actor_is_rejected_loudly():
    with pytest.raises(TypeError):
        permission_for(Action.VIEW_ORG, "owner")  # type: ignore[arg-type]
