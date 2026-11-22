"""Organisation policies (Phase 10): lock modes, the approved mode ids, the per-member spend cap.

GET returns the policy and its version (the ETag); PUT replaces it as a whole with If-Match: "v<version>". In ONE
transaction a write bumps the version, writes the audit row `policy.changed {old, new}` and enqueues `org.policy.changed`
(the full policy, keyed by org_id). Plan gating comes from core/plan_features.py. Mode ids are NOT checked for existence:
conversation-service owns modes, we only store which ones the organisation approved. billing reads the spend cap from
/internal/orgs/{id} (directory_service.org_details), not from here.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.exceptions import FeatureNotInPlan, ValidationFailed, VersionConflict
from app.events import topics
from app.events.producers import enqueue
from app.events.schemas import OrgPolicyChanged
from app.models.policy import OrgPolicy
from app.repositories.policy_repository import PolicyRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.security.models import OrgContext
from app.services import audit_service, org_gate
from app.services.me_service import features_for

MAX_APPROVED_MODES = 50
MIN_CAP_PAISE, MAX_CAP_PAISE = 100, 10_000_000
DEFAULT_VERSION = 1


@dataclass(frozen=True)
class PolicyView:
    org_id: uuid.UUID
    version: int
    lock_modes: bool
    approved_mode_ids: list[uuid.UUID]
    member_spend_cap_paise: int | None
    updated_at: datetime | None
    updated_by: uuid.UUID | None

    def snapshot(self) -> dict:
        return {"lock_modes": self.lock_modes, "approved_mode_ids": [str(m) for m in self.approved_mode_ids],
                "member_spend_cap_paise": self.member_spend_cap_paise}


def _view(org_id: uuid.UUID, row: OrgPolicy | None) -> PolicyView:
    if row is None:          # an organisation created before policies existed: the default, at the first version
        return PolicyView(org_id, DEFAULT_VERSION, False, [], None, None, None)
    return PolicyView(org_id, row.version, row.lock_modes, list(row.approved_mode_ids or []), row.member_spend_cap_paise,
                      row.updated_at, row.updated_by)


async def get_policy(session: AsyncSession, org_id: uuid.UUID) -> PolicyView:
    return _view(org_id, await PolicyRepository(session).get(org_id))


def parse_if_match(header: str | None) -> int | None:
    """`"v3"`, `v3` or `3` -> 3. None if the header is absent; ValidationFailed if it is not a version."""
    if header is None or not header.strip():
        return None
    raw = header.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"').lstrip("vV")
    if not raw.isdigit():
        raise ValidationFailed('If-Match must look like "v3"')
    return int(raw)


def validate(lock_modes: bool, approved_mode_ids: list[uuid.UUID], cap: int | None) -> list[uuid.UUID]:
    """The approved ids deduplicated (first occurrence wins); ValidationFailed for a bad policy."""
    ids = list(dict.fromkeys(approved_mode_ids))
    if len(ids) > MAX_APPROVED_MODES:
        raise ValidationFailed(f"at most {MAX_APPROVED_MODES} approved modes")
    if lock_modes and not ids:
        raise ValidationFailed("lock_modes needs at least one approved mode")
    if cap is not None and not MIN_CAP_PAISE <= cap <= MAX_CAP_PAISE:
        raise ValidationFailed(f"member_spend_cap_paise must be between {MIN_CAP_PAISE} and {MAX_CAP_PAISE}, or null")
    return ids


async def put_policy(session: AsyncSession, ctx: OrgContext, expected_version: int, lock_modes: bool,
                     approved_mode_ids: list[uuid.UUID], member_spend_cap_paise: int | None,
                     clock: Clock = system_clock) -> PolicyView:
    await org_gate.require_open(session, ctx.org_id)                      # a closed organisation: ORG_INACTIVE
    repo = PolicyRepository(session)
    row = await repo.get(ctx.org_id)
    old = _view(ctx.org_id, row)
    if old.version != expected_version:
        raise _conflict(old.version)
    ids = validate(lock_modes, approved_mode_ids, member_spend_cap_paise)
    seat = await SeatStateRepository(session).get(ctx.org_id)
    features = features_for(seat.plan_code if seat else None)
    if lock_modes and not features["lock_modes"]:
        raise FeatureNotInPlan("lock_modes is not included in this plan")
    if member_spend_cap_paise is not None and not features["spend_cap"]:
        raise FeatureNotInPlan("spend_cap (a per-member spend cap) is only included in the pay-as-you-go plan")

    now = clock.now()
    if row is None:
        await repo.add(OrgPolicy(org_id=ctx.org_id, lock_modes=lock_modes, approved_mode_ids=ids,
                                 member_spend_cap_paise=member_spend_cap_paise, version=old.version + 1,
                                 updated_by=ctx.user.id, updated_at=now))
    elif not await repo.update_if_version(ctx.org_id, expected_version, ctx.user.id, now, lock_modes=lock_modes,
                                          approved_mode_ids=ids, member_spend_cap_paise=member_spend_cap_paise):
        raise _conflict((await repo.get(ctx.org_id)).version)               # someone else got in between
    new = _view(ctx.org_id, await repo.get(ctx.org_id))
    await audit_service.record(session, ctx.user, constants.AUDIT_POLICY_CHANGED, "policy", ctx.org_id,
                               {"old": old.snapshot() | {"version": old.version}, "new": new.snapshot() | {"version": new.version}},
                               org_id=ctx.org_id, clock=clock)
    event = OrgPolicyChanged.create(clock, org_id=str(ctx.org_id), version=new.version, lock_modes=new.lock_modes,
                                    approved_mode_ids=[str(m) for m in new.approved_mode_ids],
                                    member_spend_cap_paise=new.member_spend_cap_paise)
    await enqueue(session, topics.ORG_POLICY_CHANGED, str(ctx.org_id), event, clock)
    return new


def _conflict(current: int) -> VersionConflict:
    error = VersionConflict(f"the policy is at version {current}; fetch it again")
    error.extra = {"current_version": current}
    return error
