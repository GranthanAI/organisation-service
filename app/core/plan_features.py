"""The org plan table (docs/PHASES.md Contracts 6), keyed by plan code. This is the ONLY place it lives.

billing's `plans.yaml` has no feature flags for organisations yet; until it does, this file holds them (the
minimum seats below are billing's `min_seats` for org_pro and org_max, per variant).
"""

from dataclasses import dataclass

from app.core.enums import OrgType, PlanCode


@dataclass(frozen=True)
class PlanFeatures:
    code: PlanCode
    min_seats: dict[OrgType, int | None]   # None: not sold by seat (the pilot has a fixed cap, payg has no cap)
    lock_modes: bool
    shared_modes_max: int | None           # None = unlimited
    usage_reports: bool
    member_spend_cap: bool


PLANS: dict[PlanCode, PlanFeatures] = {
    PlanCode.PILOT: PlanFeatures(PlanCode.PILOT, {OrgType.COLLEGE: None, OrgType.COMPANY: None},
                                 lock_modes=False, shared_modes_max=3, usage_reports=False, member_spend_cap=False),
    PlanCode.ORG_PRO: PlanFeatures(PlanCode.ORG_PRO, {OrgType.COLLEGE: 50, OrgType.COMPANY: 5},
                                   lock_modes=True, shared_modes_max=20, usage_reports=True, member_spend_cap=False),
    PlanCode.ORG_MAX: PlanFeatures(PlanCode.ORG_MAX, {OrgType.COLLEGE: 10, OrgType.COMPANY: 5},
                                   lock_modes=True, shared_modes_max=50, usage_reports=True, member_spend_cap=False),
    PlanCode.PAYG: PlanFeatures(PlanCode.PAYG, {OrgType.COLLEGE: None, OrgType.COMPANY: None},
                                lock_modes=True, shared_modes_max=None, usage_reports=True, member_spend_cap=True),
}

PAID_PLANS = frozenset({PlanCode.ORG_PRO, PlanCode.ORG_MAX, PlanCode.PAYG})


def features(plan: PlanCode) -> PlanFeatures:
    return PLANS[plan]


def min_seats(plan: PlanCode, variant: OrgType) -> int | None:
    """Fewest seats that can be sold on `plan` for `variant`; None if the plan is not sold by seat."""
    return PLANS[plan].min_seats[variant]
