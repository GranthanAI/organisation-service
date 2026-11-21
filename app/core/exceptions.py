"""Domain errors. Each carries the problem+json `code` (docs/PHASES.md P3c; the HTTP mapping arrives in Phase 3)."""

from app.core.enums import JoinDecision


class DomainError(Exception):
    code = "DOMAIN_ERROR"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        self.detail = detail


class ForbiddenRole(DomainError):
    code = "FORBIDDEN_ROLE"


class IllegalTransition(DomainError):
    code = "VALIDATION_ERROR"


class LastOwner(DomainError):
    code = "LAST_OWNER"


class IllegalRoleChange(DomainError):
    code = "VALIDATION_ERROR"


class InvalidTransferTarget(DomainError):
    code = "VALIDATION_ERROR"


class InvalidEmail(DomainError, ValueError):
    code = "VALIDATION_ERROR"


class AlreadyInOrg(DomainError):
    code = "ALREADY_IN_ORG"


class InviteAlreadyOpen(DomainError):
    code = "INVITE_ALREADY_OPEN"


class DomainTaken(DomainError):
    code = "DOMAIN_TAKEN"


class SeatsFull(DomainError):
    code = "SEATS_FULL"


class PilotFull(DomainError):
    code = "PILOT_FULL"


class OrgInactive(DomainError):
    code = "ORG_INACTIVE"


class InviteInvalid(DomainError):
    code = "INVITE_INVALID"


class InviteExpired(DomainError):
    code = "INVITE_EXPIRED"


class NotFound(DomainError):
    """A row the caller may not know exists, or does not. P3c lists no NOT_FOUND code; this one is 404 (Phase 3)."""

    code = "NOT_FOUND"


_JOIN_ERRORS: dict[JoinDecision, type[DomainError] | None] = {
    JoinDecision.OK: None,
    JoinDecision.SEATS_FULL: SeatsFull,
    JoinDecision.PILOT_FULL: PilotFull,
    JoinDecision.ORG_INACTIVE: OrgInactive,
}


def join_decision_error(decision: JoinDecision) -> DomainError | None:
    """The exception for a refused join, or None when the join is allowed."""
    error_class = _JOIN_ERRORS[decision]
    return error_class() if error_class else None
