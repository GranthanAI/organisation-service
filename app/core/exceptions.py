"""Domain errors. Each carries the problem+json `code` (HTTP mapping arrives in Phase 3)."""


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
