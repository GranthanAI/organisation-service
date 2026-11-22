"""GET /org/v1/orgs/{org_id}/usage?from=YYYY-MM-DD&to=YYYY-MM-DD&format=json|csv: per-member usage from billing, with names.

Owner and admin (VIEW_USAGE) on a plan with usage_reports (FEATURE_NOT_IN_PLAN otherwise). Dates are IST days."""

import asyncio
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_db_session
from app.core.clock import Clock
from app.schemas.org_v1 import UsageMemberOut, UsageOut, UsageTotalsOut
from app.security.dependencies import require_org_role
from app.security.models import OrgContext
from app.services import usage_service
from app.services.permission_rules import Action

router = APIRouter(prefix="/orgs/{org_id}", tags=["Usage"])


@router.get("/usage", response_model=UsageOut)
async def usage(from_: date = Query(alias="from"), to: date = Query(), format: Literal["json", "csv"] = "json",
                ctx: OrgContext = Depends(require_org_role(Action.VIEW_USAGE)),
                session: AsyncSession = Depends(get_db_session), clock: Clock = Depends(get_clock)):
    report = await usage_service.usage_report(session, ctx.org_id, from_, to, clock)
    if format == "csv":
        slug = await usage_service.org_slug(session, ctx.org_id)
        content = await asyncio.to_thread(usage_service.render_csv, report)
        return Response(content=content, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{usage_service.filename(slug, from_, to)}"'})
    return UsageOut(
        **{"from": report.start, "to": report.end},
        totals=UsageTotalsOut(messages=report.messages, billable_units=report.billable_units, by_model_group=report.by_model_group),
        members=[UsageMemberOut(user_id=m.user_id, name=m.name, email=m.email, role=m.role, messages=m.messages,
                                billable_units=m.billable_units, by_model_group=m.by_model_group) for m in report.members])
