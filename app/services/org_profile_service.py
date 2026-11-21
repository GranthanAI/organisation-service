"""An organisation as its own members see it (Phase 6): the details, and editing them (owner or admin)."""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.exceptions import NotFound, ValidationFailed
from app.models.organisation import Organisation
from app.repositories.org_repository import OrgRepository
from app.security.models import OrgContext
from app.services import audit_service, org_gate
from app.services.email_rules import normalize_email
from app.services.org_service import validate_gstin_and_state

EDITABLE = ("name", "legal_name", "gstin", "state_code", "billing_email")
NULLABLE = {"legal_name", "gstin", "state_code"}


async def get_details(session: AsyncSession, org_id: uuid.UUID) -> Organisation:
    org = await OrgRepository(session).get(org_id)
    if org is None:
        raise NotFound("Organisation not found")
    return org


def _snapshot(value):
    return value.isoformat() if isinstance(value, datetime) else value


async def patch_details(session: AsyncSession, ctx: OrgContext, changes: dict, clock: Clock = system_clock) -> Organisation:
    """Apply the fields that were sent (`changes` holds only those). The slug never changes. Audits `org.details_changed`
    with only the fields whose value actually changed, old and new. An empty `changes` is a 422."""
    if not changes:
        raise ValidationFailed("send at least one field to change")
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise ValidationFailed(f"cannot change: {', '.join(sorted(unknown))}")
    orgs = OrgRepository(session)
    org = await orgs.get_for_update(ctx.org_id)
    if org is None:
        raise NotFound("Organisation not found")
    await org_gate.require_open(session, ctx.org_id)

    new = dict(changes)
    if "name" in new:
        name = (new["name"] or "").strip()
        if not 3 <= len(name) <= 120:
            raise ValidationFailed("name must be 3 to 120 characters")
        new["name"] = name
    if "billing_email" in new:
        if not new["billing_email"]:
            raise ValidationFailed("billing_email cannot be empty")
        new["billing_email"] = normalize_email(new["billing_email"])
    # one validation function for GSTIN and state (the same one create uses), applied to the values as they will be
    if "gstin" in new or "state_code" in new:      # existing values are only re-checked when one of them is edited
        merged_gstin = new["gstin"] if "gstin" in new else org.gstin
        merged_state = new["state_code"] if "state_code" in new else org.state_code
        gstin, state = validate_gstin_and_state(merged_gstin, merged_state)
        if "gstin" in new:
            new["gstin"] = gstin
        if "state_code" in new:
            new["state_code"] = state
    for field in NULLABLE:
        if field in new and isinstance(new[field], str) and not new[field].strip():
            new[field] = None
    diff = {}
    for field, value in new.items():
        old = getattr(org, field)
        if old != value:
            diff[field] = {"old": _snapshot(old), "new": _snapshot(value)}
            setattr(org, field, value)
    if not diff:
        return org                                   # nothing changed: no write, no audit
    org.updated_at = clock.now()
    await orgs.save(org)
    await audit_service.record(session, ctx.user, constants.AUDIT_ORG_DETAILS_CHANGED, "org", org.id, diff,
                               org_id=org.id, clock=clock)
    return org
