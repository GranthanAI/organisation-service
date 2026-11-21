"""Membership rules: last owner, role changes, ownership transfer. Pure: stdlib and app/core only.

Callers pass counts and roles they have already read (inside the join/leave transaction); nothing here does I/O.
Who is allowed to attempt an action is permission_rules' job (a sibling rules file, which this file may not import):
callers run permission_rules.check(CHANGE_ROLE / TRANSFER_OWNERSHIP / REMOVE_MEMBER, actor) first.
"""

from dataclasses import dataclass

from app.core.enums import EndReason, MembershipStatus, Role
from app.core.exceptions import IllegalRoleChange, InvalidTransferTarget, LastOwner


@dataclass(frozen=True)
class EndCheck:
    # True when this ending removes the last active owner. Only system endings can do that; the caller then writes
    # the `org.owner_missing` audit row for staff to fix (Phase 4).
    owner_missing: bool = False


# A person leaving or being removed is blocked from orphaning the org; the system is not blocked.
_USER_INITIATED = frozenset({EndReason.LEFT, EndReason.REMOVED})


def check_can_end(target_role: Role, active_owner_count: int, reason: EndReason) -> EndCheck:
    """Leaving or being removed must not leave zero active owners (LastOwner). `user_deleted` and `org_closed`
    always pass, because the account is gone or the whole org is ending; they report `owner_missing` instead."""
    ends_last_owner = target_role is Role.OWNER and active_owner_count <= 1
    if ends_last_owner and reason in _USER_INITIATED:
        raise LastOwner("the last owner cannot leave or be removed; transfer ownership first")
    return EndCheck(owner_missing=ends_last_owner and reason not in _USER_INITIATED)


def check_role_change(
    target_role: Role,
    new_role: Role,
    active_owner_count: int,
) -> None:
    """Change an admin/member role. Ownership moves only by transfer."""
    if new_role is target_role:
        raise IllegalRoleChange(f"member already has role {new_role.value}")
    if new_role is Role.OWNER:
        raise IllegalRoleChange("ownership changes only through transfer-ownership")
    if target_role is Role.OWNER:
        # Demoting an owner is only possible while another active owner remains.
        if active_owner_count <= 1:
            raise LastOwner("the last owner cannot be demoted; transfer ownership first")


@dataclass(frozen=True)
class TransferPlan:
    new_owner_role: Role
    old_owner_role: Role


def plan_transfer(
    current_owner_role: Role,
    target_role: Role,
    target_status: MembershipStatus,
) -> TransferPlan:
    """The target must be an active admin; the old owner becomes admin. Returns the resulting roles."""
    if current_owner_role is not Role.OWNER:
        raise InvalidTransferTarget("only an owner can hand over ownership")
    if target_status is not MembershipStatus.ACTIVE:
        raise InvalidTransferTarget("the new owner must be an active member")
    if target_role is not Role.ADMIN:
        raise InvalidTransferTarget("the new owner must be an admin of the organisation")
    return TransferPlan(new_owner_role=Role.OWNER, old_owner_role=Role.ADMIN)
