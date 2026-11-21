"""Write one audit_log row in the CALLER's session.

It never opens a transaction or commits: the row commits or rolls back with the use case that wrote it. Every write
use case from Phase 5 on calls it. Never put a token, secret or password in `details` (the keys are refused).
"""

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, system_clock
from app.core.constants import AUDIT_DETAILS_MAX_BYTES, AUDIT_FORBIDDEN_KEY_PARTS
from app.middleware.correlation import get_correlation_id
from app.models.audit_log import AuditLog
from app.repositories.audit_repository import AuditRepository
from app.security.models import CurrentUser, ServiceCaller
from app.utils.ids import uuid7

SYSTEM = "system"
Actor = CurrentUser | ServiceCaller | str


def _check_keys(value: Any, path: str = "details") -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if any(part in str(key).lower() for part in AUDIT_FORBIDDEN_KEY_PARTS):
                raise ValueError(f"audit details may not contain a key like {key!r} (tokens, secrets and passwords)")
            _check_keys(inner, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_keys(item, path)


def validate_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """JSON-serialisable, at most 8 KB, no key containing token, secret or password (any depth). ValueError if not."""
    details = details or {}
    _check_keys(details)
    try:
        encoded = json.dumps(details, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("audit details must be JSON-serialisable") from exc
    if len(encoded.encode("utf-8")) > AUDIT_DETAILS_MAX_BYTES:
        raise ValueError(f"audit details are larger than {AUDIT_DETAILS_MAX_BYTES} bytes")
    return details


def actor_fields(actor: Actor, staff: bool) -> tuple[uuid.UUID | None, str, dict[str, Any]]:
    """(actor_id, actor_kind, extra details). `staff=True` marks a CurrentUser acting through an admin route."""
    if isinstance(actor, CurrentUser):
        return actor.id, ("staff" if staff else "user"), {}
    if isinstance(actor, ServiceCaller):
        return None, "service", {"service": actor.name}
    if actor == SYSTEM:
        return None, "system", {}
    raise TypeError("actor must be a CurrentUser, a ServiceCaller or 'system'")


async def record(
    session: AsyncSession,
    actor: Actor,
    action: str,
    target_type: str | None,
    target_id: str | uuid.UUID | None,
    details: dict[str, Any] | None = None,
    reason: str | None = None,
    *,
    org_id: uuid.UUID | None = None,
    staff: bool = False,
    clock: Clock = system_clock,
) -> AuditLog:
    actor_id, actor_kind, extra = actor_fields(actor, staff)
    body = validate_details(details)
    if extra:
        body = {**body, **extra}
    entry = AuditLog(
        id=uuid7(), org_id=org_id, actor_id=actor_id, actor_kind=actor_kind, action=action, target_type=target_type,
        target_id=None if target_id is None else str(target_id), details=body, reason=reason,
        correlation_id=get_correlation_id() or None, at=clock.now(),
    )
    return await AuditRepository(session).add(entry)
