import pytest

from app.core.enums import EndReason, MembershipStatus, Role
from app.core.exceptions import IllegalRoleChange, InvalidTransferTarget, LastOwner
from app.services.membership_rules import check_can_end, check_role_change, plan_transfer

# --- last owner ---
USER_INITIATED = [EndReason.LEFT, EndReason.REMOVED]
SYSTEM = [EndReason.USER_DELETED, EndReason.ORG_CLOSED]


@pytest.mark.parametrize("reason", USER_INITIATED)
def test_sole_owner_cannot_leave_or_be_removed(reason):
    with pytest.raises(LastOwner):
        check_can_end(Role.OWNER, 1, reason)


@pytest.mark.parametrize("reason", USER_INITIATED)
def test_zero_owners_counts_as_last_owner(reason):
    with pytest.raises(LastOwner):
        check_can_end(Role.OWNER, 0, reason)


@pytest.mark.parametrize("reason", list(EndReason))
def test_owner_can_end_when_another_owner_remains(reason):
    assert check_can_end(Role.OWNER, 2, reason).owner_missing is False


@pytest.mark.parametrize("reason", list(EndReason))
@pytest.mark.parametrize("role", [Role.ADMIN, Role.MEMBER])
def test_non_owners_can_always_end(role, reason):
    assert check_can_end(role, 1, reason).owner_missing is False
    assert check_can_end(role, 0, reason).owner_missing is False


@pytest.mark.parametrize("reason", SYSTEM)
def test_system_endings_pass_and_flag_the_missing_owner(reason):
    assert check_can_end(Role.OWNER, 1, reason).owner_missing is True


def test_end_reason_values_match_the_member_left_event():
    assert {r.value for r in EndReason} == {"left", "removed", "user_deleted", "org_closed"}


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
