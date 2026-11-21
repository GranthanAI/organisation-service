"""Who may do what. Pure: stdlib and app/core only.

One table per actor kind maps (Action, role) to Allowed / Denied / OwnRoleOnly, so a test can walk every cell.
OwnRoleOnly means "allowed, but only against a target whose role ranks below the actor's" (an admin removes members,
never another admin or the owner). `check` fails closed when the target role is missing.
"""

from enum import Enum

from app.core.enums import Role, StaffRole
from app.core.exceptions import ForbiddenRole


class Action(str, Enum):
    VIEW_ORG = "view_org"
    INVITE = "invite"
    REVOKE_INVITE = "revoke_invite"
    CSV_UPLOAD = "csv_upload"
    MANAGE_LINKS = "manage_links"
    REMOVE_MEMBER = "remove_member"
    CHANGE_ROLE = "change_role"
    TRANSFER_OWNERSHIP = "transfer_ownership"
    CLOSE_ORG = "close_org"
    EDIT_DETAILS = "edit_details"
    MANAGE_DOMAINS = "manage_domains"
    EDIT_POLICIES = "edit_policies"
    VIEW_USAGE = "view_usage"
    VIEW_AUDIT = "view_audit"
    LEAVE = "leave"
    STAFF_CREATE_ORG = "staff_create_org"
    STAFF_SUSPEND = "staff_suspend"
    STAFF_EXTEND_PILOT = "staff_extend_pilot"
    STAFF_MANUAL_DOMAIN_VERIFY = "staff_manual_domain_verify"
    STAFF_SET_SEATS = "staff_set_seats"


class Permission(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    OWN_ROLE_ONLY = "own_role_only"


A, D, O = Permission.ALLOWED, Permission.DENIED, Permission.OWN_ROLE_ONLY

# action: (owner, admin, member, staff support, staff admin)
_MATRIX: dict[Action, tuple[Permission, Permission, Permission, Permission, Permission]] = {
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
    # The owner may leave only after transferring ownership; that is the last-owner rule in membership_rules.
    Action.LEAVE:                      (A, A, A, D, D),
    Action.STAFF_CREATE_ORG:           (D, D, D, D, A),
    Action.STAFF_SUSPEND:              (D, D, D, D, A),
    Action.STAFF_EXTEND_PILOT:         (D, D, D, D, A),
    Action.STAFF_MANUAL_DOMAIN_VERIFY: (D, D, D, D, A),
    Action.STAFF_SET_SEATS:            (D, D, D, D, A),
}

ORG_TABLE: dict[tuple[Action, Role], Permission] = {}
STAFF_TABLE: dict[tuple[Action, StaffRole], Permission] = {}
for _action, (_o, _a, _m, _sup, _adm) in _MATRIX.items():
    ORG_TABLE[(_action, Role.OWNER)] = _o
    ORG_TABLE[(_action, Role.ADMIN)] = _a
    ORG_TABLE[(_action, Role.MEMBER)] = _m
    STAFF_TABLE[(_action, StaffRole.SUPPORT)] = _sup
    STAFF_TABLE[(_action, StaffRole.ADMIN)] = _adm

_RANK = {Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}


def permission_for(action: Action, actor: Role | StaffRole) -> Permission:
    """The table cell. Dispatches on the enum class: Role.ADMIN and StaffRole.ADMIN compare equal as strings."""
    if isinstance(actor, StaffRole):
        return STAFF_TABLE[(action, actor)]
    if isinstance(actor, Role):
        return ORG_TABLE[(action, actor)]
    raise TypeError(f"actor must be a Role or StaffRole, got {type(actor).__name__}")


def check(action: Action, actor: Role | StaffRole, target_role: Role | None = None) -> None:
    """Raise ForbiddenRole unless the actor may do the action. `target_role` is needed for OwnRoleOnly cells."""
    permission = permission_for(action, actor)
    if permission is Permission.ALLOWED:
        return
    if permission is Permission.OWN_ROLE_ONLY and isinstance(actor, Role) and target_role is not None:
        if _RANK[target_role] < _RANK[actor]:
            return
    raise ForbiddenRole(f"{actor.value} may not {action.value}")
