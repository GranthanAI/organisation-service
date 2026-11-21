"""Who is calling. Built from verified JWT claims (users) or a matched service token (services)."""

import uuid
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.core.enums import Role, StaffRole
from app.core.exceptions import Unauthenticated
from app.services import permission_rules
from app.services.email_rules import normalize_email
from app.services.permission_rules import Action


def staff_role_from_roles(roles: list[str]) -> StaffRole | None:
    """`admin` implies `billing_admin` and `support`. `billing_admin` alone is not staff here."""
    if "admin" in roles:
        return StaffRole.ADMIN
    if "support" in roles:
        return StaffRole.SUPPORT
    return None


class CurrentUser(BaseModel):
    id: uuid.UUID
    email: str
    roles: list[str] = Field(default_factory=list)
    jti: str = Field(repr=False)   # never logged
    staff_role: StaffRole | None = None

    @classmethod
    def from_claims(cls, claims: dict) -> "CurrentUser":
        """Raises Unauthenticated (no detail) if a claim is malformed."""
        try:
            roles = claims.get("roles") or []
            if isinstance(roles, str):
                roles = [roles]
            if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
                raise ValueError("roles")
            jti = claims["jti"]
            if not isinstance(jti, str) or not jti:
                raise ValueError("jti")
            return cls(id=uuid.UUID(str(claims["sub"])), email=normalize_email(str(claims["email"])), roles=roles,
                       jti=jti, staff_role=staff_role_from_roles(roles))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise Unauthenticated() from exc


@dataclass(frozen=True)
class ServiceCaller:
    name: str


@dataclass(frozen=True)
class OrgContext:
    """The caller's active membership in the org named in the path."""

    org_id: uuid.UUID
    user: CurrentUser
    role: Role
    membership_id: uuid.UUID
    action: Action

    def check_target(self, target_role: Role) -> None:
        """Run the matrix against the person being acted on (an admin may remove members only).
        `require_org_role` lets OwnRoleOnly actions through because only the use case knows the target; call this
        for remove and role changes. Raises ForbiddenRole."""
        permission_rules.check(self.action, self.role, target_role)
