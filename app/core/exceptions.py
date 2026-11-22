"""Domain errors. Each carries the problem+json `code` (docs/PHASES.md P3c; the HTTP mapping arrives in Phase 3)."""

from app.core.enums import JoinDecision


class DomainError(Exception):
    code = "DOMAIN_ERROR"
    headers: dict[str, str] | None = None   # extra response headers (Retry-After, WWW-Authenticate)
    extra: dict | None = None               # extra fields for the problem+json body (VERSION_CONFLICT: current_version)

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


class JoinRequestPending(DomainError):
    code = "JOIN_REQUEST_PENDING"


class SeatsFull(DomainError):
    code = "SEATS_FULL"


class PilotFull(DomainError):
    code = "PILOT_FULL"


class OrgInactive(DomainError):
    code = "ORG_INACTIVE"


class SeatsManagedByBilling(DomainError):
    """SEAT_SOURCE=billing: staff cannot set seats here (Phase 10 reads them from billing)."""

    code = "SEATS_MANAGED_BY_BILLING"


class UnknownUser(DomainError):
    """A user id we have never seen (no known_users row). Logged-in users are always known (K2)."""

    code = "UNKNOWN_USER"


class InviteInvalid(DomainError):
    code = "INVITE_INVALID"


class InviteExpired(DomainError):
    code = "INVITE_EXPIRED"


class NotFound(DomainError):
    """A row the caller may not know exists, or does not. P3c lists no NOT_FOUND code; this one is 404 (Phase 3)."""

    code = "NOT_FOUND"


class Unauthenticated(DomainError):
    """Missing, invalid, expired, revoked or deleted-user credentials. The detail never says which."""

    code = "UNAUTHENTICATED"
    headers = {"WWW-Authenticate": "Bearer"}

    def __init__(self) -> None:
        super().__init__("Authentication required")


class UpstreamUnavailable(DomainError):
    code = "UPSTREAM_UNAVAILABLE"


class RateLimited(DomainError):
    code = "RATE_LIMITED"

    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many requests")
        self.retry_after = max(1, int(retry_after))
        self.headers = {"Retry-After": str(self.retry_after)}


class IdempotencyMismatch(DomainError):
    code = "IDEMPOTENCY_MISMATCH"


class IdempotencyInProgress(DomainError):
    code = "IDEMPOTENCY_IN_PROGRESS"

    def __init__(self, retry_after: int = 1) -> None:
        super().__init__("A request with this Idempotency-Key is still being processed")
        self.retry_after = max(1, int(retry_after))
        self.headers = {"Retry-After": str(self.retry_after)}


class ValidationFailed(DomainError):
    code = "VALIDATION_ERROR"


class EmailNotVerified(DomainError):
    code = "EMAIL_NOT_VERIFIED"


class InviteEmailMismatch(DomainError):
    code = "INVITE_EMAIL_MISMATCH"


class FeatureNotInPlan(DomainError):
    code = "FEATURE_NOT_IN_PLAN"


class PreconditionRequired(DomainError):
    code = "PRECONDITION_REQUIRED"


class VersionConflict(DomainError):
    code = "VERSION_CONFLICT"


class PublicEmailDomain(DomainError):
    code = "PUBLIC_EMAIL_DOMAIN"


class RequestTooLarge(DomainError):
    code = "REQUEST_TOO_LARGE"


class CsvInvalid(DomainError):
    code = "CSV_INVALID"


class PaymentDue(DomainError):
    code = "PAYMENT_DUE"


_JOIN_ERRORS: dict[JoinDecision, type[DomainError] | None] = {
    JoinDecision.OK: None,
    JoinDecision.SEATS_FULL: SeatsFull,
    JoinDecision.PILOT_FULL: PilotFull,
    JoinDecision.ORG_INACTIVE: OrgInactive,
    JoinDecision.PAYMENT_DUE: PaymentDue,
}


def join_decision_error(decision: JoinDecision) -> DomainError | None:
    """The exception for a refused join, or None when the join is allowed."""
    error_class = _JOIN_ERRORS[decision]
    return error_class() if error_class else None
