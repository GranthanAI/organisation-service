import pytest

from app.core.enums import MembershipStatus, Role
from app.core.exceptions import IllegalRoleChange, InvalidTransferTarget, LastOwner
from app.services.membership_rules import check_can_end, check_role_change, plan_transfer


# --- last owner ---
def test_sole_owner_cannot_end():
    with pytest.raises(LastOwner):
        check_can_end(Role.OWNER, active_owner_count=1)


def test_owner_can_end_when_another_owner_remains():
    check_can_end(Role.OWNER, active_owner_count=2)


@pytest.mark.parametrize("role", [Role.ADMIN, Role.MEMBER])
def test_non_owners_can_always_end(role):
    check_can_end(role, active_owner_count=1)


def test_zero_owners_counts_as_last_owner():
    with pytest.raises(LastOwner):
        check_can_end(Role.OWNER, active_owner_count=0)


# --- role changes ---
@pytest.mark.parametrize("target, new", [(Role.MEMBER, Role.ADMIN), (Role.ADMIN, Role.MEMBER)])
def test_admin_member_changes_are_allowed(target, new):
    check_role_change(target, new, active_owner_count=1)


@pytest.mark.parametrize("role", list(Role))
def test_same_role_is_refused(role):
    with pytest.raises(IllegalRoleChange):
        check_role_change(role, role, active_owner_count=2)


@pytest.mark.parametrize("target", [Role.ADMIN, Role.MEMBER])
def test_cannot_promote_to_owner_by_role_change(target):
    with pytest.raises(IllegalRoleChange):
        check_role_change(target, Role.OWNER, active_owner_count=1)


def test_last_owner_cannot_be_demoted():
    for new in (Role.ADMIN, Role.MEMBER):
        with pytest.raises(LastOwner):
            check_role_change(Role.OWNER, new, active_owner_count=1)


def test_owner_can_be_demoted_while_another_owner_remains():
    check_role_change(Role.OWNER, Role.ADMIN, active_owner_count=2)


# --- transfer ---
def test_transfer_to_active_admin_makes_old_owner_admin():
    plan = plan_transfer(Role.OWNER, Role.ADMIN, MembershipStatus.ACTIVE)
    assert plan.new_owner_role is Role.OWNER
    assert plan.old_owner_role is Role.ADMIN


@pytest.mark.parametrize("target_role", [Role.MEMBER, Role.OWNER])
def test_transfer_target_must_be_admin(target_role):
    with pytest.raises(InvalidTransferTarget):
        plan_transfer(Role.OWNER, target_role, MembershipStatus.ACTIVE)


@pytest.mark.parametrize("status", [MembershipStatus.LEFT, MembershipStatus.REMOVED])
def test_transfer_target_must_be_active(status):
    with pytest.raises(InvalidTransferTarget):
        plan_transfer(Role.OWNER, Role.ADMIN, status)


@pytest.mark.parametrize("current", [Role.ADMIN, Role.MEMBER])
def test_only_an_owner_hands_over(current):
    with pytest.raises(InvalidTransferTarget):
        plan_transfer(current, Role.ADMIN, MembershipStatus.ACTIVE)
