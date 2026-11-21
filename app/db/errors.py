"""Maps database constraint violations to domain errors, by constraint (index) name.

Anything not listed here is not translated: `map_integrity_error` returns None and the caller re-raises unchanged.
"""

import re

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AlreadyInOrg, DomainError, DomainTaken, InviteAlreadyOpen

CONSTRAINT_ERRORS: dict[str, type[DomainError]] = {
    "ux_memberships_one_active_org": AlreadyInOrg,
    "ux_invites_open_email": InviteAlreadyOpen,
    "ux_org_domains_verified": DomainTaken,
}

_NAME_IN_MESSAGE = re.compile(r'constraint "([^"]+)"|index "([^"]+)"')


def constraint_name(exc: BaseException) -> str | None:
    """The violated constraint's name: asyncpg exposes it as `constraint_name` on the driver error (reachable through
    SQLAlchemy's `orig` and its `__cause__`); the message text is the fallback."""
    candidates = [exc, getattr(exc, "orig", None)]
    candidates += [getattr(c, "__cause__", None) for c in list(candidates)]
    for candidate in candidates:
        name = getattr(candidate, "constraint_name", None)
        if name:
            return name
    match = _NAME_IN_MESSAGE.search(str(getattr(exc, "orig", exc)))
    return (match.group(1) or match.group(2)) if match else None


def map_integrity_error(exc: IntegrityError) -> DomainError | None:
    """A domain error for a known constraint, or None for anything else."""
    error_class = CONSTRAINT_ERRORS.get(constraint_name(exc) or "")
    return error_class() if error_class else None
