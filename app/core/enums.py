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
