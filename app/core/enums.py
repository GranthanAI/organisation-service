"""Domain enums. Values are what is stored in the database and sent in events.

Note: Role and StaffRole are both str enums and both have an "admin" value, so Role.ADMIN == StaffRole.ADMIN is True
(str equality). Never mix them in one dict or set; dispatch on isinstance (see services/permission_rules.py).
"""

from enum import Enum


class OrgType(str, Enum):
    COLLEGE = "college"
    COMPANY = "company"


class OrgStatus(str, Enum):
    PILOT = "pilot"
    ACTIVE = "active"
    PILOT_EXPIRED = "pilot_expired"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    LEFT = "left"
    REMOVED = "removed"


class EndReason(str, Enum):
    """Why a membership ended (memberships.end_reason, and `reason` on org.member.left)."""

    LEFT = "left"
    REMOVED = "removed"
    USER_DELETED = "user_deleted"
    ORG_CLOSED = "org_closed"


class PlanCode(str, Enum):
    """seat_state.plan_code."""

    PILOT = "pilot"
    ORG_PRO = "org_pro"
    ORG_MAX = "org_max"
    PAYG = "payg"


class SeatSource(str, Enum):
    STAFF = "staff"
    BILLING = "billing"


class SeatStatus(str, Enum):
    """seat_state.status: billing's subscription statuses. Staff-sourced pilot and paid rows use ACTIVE."""

    INCOMPLETE = "incomplete"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELLING = "cancelling"
    ENDED = "ended"


class InviteRevokeReason(str, Enum):
    """invites.revoke_reason: set together with revoked_at."""

    REVOKED = "revoked"
    EXPIRED = "expired"
    RESENT = "resent"
    USER_DELETED = "user_deleted"


class JoinSource(str, Enum):
    STAFF = "staff"
    INVITE = "invite"
    CSV = "csv"
    LINK = "link"
    DOMAIN = "domain"


class InviteKind(str, Enum):
    EMAIL = "email"
    CSV = "csv"
    LINK = "link"


class StaffRole(str, Enum):
    """GranthanAI staff, from the JWT `roles` claim. `admin` implies `support`; `support` is read-only."""

    SUPPORT = "support"
    ADMIN = "admin"


class JoinDecision(str, Enum):
    """Result of seat_rules.can_join."""

    OK = "ok"
    SEATS_FULL = "seats_full"
    PILOT_FULL = "pilot_full"
    ORG_INACTIVE = "org_inactive"
