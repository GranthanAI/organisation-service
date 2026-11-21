"""The ONE map from problem+json `code` to HTTP status (docs/PHASES.md P3c, plus the codes Phase 3 adds).

`tests/api/test_problem_json.py` checks that every DomainError subclass has an entry here.
"""

INTERNAL_ERROR = "INTERNAL_ERROR"   # an unexpected exception: 500, no internals in the body

ERROR_STATUS: dict[str, int] = {
    # 401
    "UNAUTHENTICATED": 401,
    # 403
    "INVITE_EMAIL_MISMATCH": 403,
    "EMAIL_NOT_VERIFIED": 403,
    "FEATURE_NOT_IN_PLAN": 403,
    "FORBIDDEN_ROLE": 403,
    # 404 / 410
    "NOT_FOUND": 404,
    "INVITE_INVALID": 404,
    "INVITE_EXPIRED": 410,
    # 409
    "SEATS_FULL": 409,
    "PILOT_FULL": 409,
    "ALREADY_IN_ORG": 409,
    "LAST_OWNER": 409,
    "ORG_INACTIVE": 409,
    "INVITE_ALREADY_OPEN": 409,
    "DOMAIN_TAKEN": 409,
    "JOIN_REQUEST_PENDING": 409,
    "SEATS_MANAGED_BY_BILLING": 409,
    "IDEMPOTENCY_IN_PROGRESS": 409,
    # 412
    "VERSION_CONFLICT": 412,
    # 422
    "PUBLIC_EMAIL_DOMAIN": 422,
    "CSV_INVALID": 422,
    "VALIDATION_ERROR": 422,
    "IDEMPOTENCY_MISMATCH": 422,
    # 429 / 5xx
    "RATE_LIMITED": 429,
    "UPSTREAM_UNAVAILABLE": 503,
    "DOMAIN_ERROR": 500,        # a DomainError subclass that forgot to set its code is a bug, not a client error
    INTERNAL_ERROR: 500,
}

# Statuses of plain HTTP errors (unknown route, wrong method) and the code each gets.
HTTP_STATUS_CODES: dict[int, str] = {
    401: "UNAUTHENTICATED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
}
