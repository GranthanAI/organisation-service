"""Email domains, join requests and the domain match (Phase 9). Domain routes need DOMAINS (owner, admin); join requests
need MANAGE_JOIN_REQUESTS; the user's own routes (match, request, cancel) need only a login. Writes use IdempotentRoute."""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_clock, get_db_session, get_redis_client, get_session_factory_dep
from app.api.idempotent_route import IdempotentRoute
from app.core.clock import Clock
from app.core.constants import RATE_DOMAIN_VERIFY
from app.core.exceptions import NotFound
from app.db.session import unit_of_work
from app.middleware.exception_handler import problem_response
from app.middleware.rate_limit import hit_fail_open
from app.schemas.org_v1 import (
    DnsRecordOut,
    DomainAddedOut,
    DomainAddIn,
    DomainListOut,
    DomainMatchOut,
    DomainOut,
    DomainPatchIn,
    JoinRequestCreatedOut,
    JoinRequestDecisionOut,
    JoinRequestPageOut,
    JoinRequestRowOut,
)
from app.security.dependencies import get_current_user, require_org_role
from app.security.models import CurrentUser, OrgContext
from app.services import domain_service, join_request_service, permission_rules
from app.services.permission_rules import Action
from app.utils.pagination import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(tags=["Domains"], route_class=IdempotentRoute)


def record_out(domain) -> DnsRecordOut:
    record = domain_service.dns_record(domain)
    return DnsRecordOut(type=record.type, host=record.host, value=record.value)


def domain_out(domain, with_record: bool) -> DomainOut:
    status = domain_service.status_of(domain)
    return DomainOut(domain=domain.domain, status=status, include_subdomains=domain.include_subdomains,
                     auto_join=domain.auto_join, verified_at=domain.verified_at, verify_method=domain.verify_method,
                     added_at=domain.added_at, dns_record=record_out(domain) if with_record and status == "pending" else None)


# ---------------------------------------------------------------------------------------------------- domains
@router.post("/orgs/{org_id}/domains", status_code=201, response_model=DomainAddedOut)
async def add_domain(body: DomainAddIn, ctx: OrgContext = Depends(require_org_role(Action.MANAGE_DOMAINS)),
                     factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        row = await domain_service.add_domain(session, ctx, body.domain, body.include_subdomains, clock)
    return DomainAddedOut(domain=row.domain, status="pending", include_subdomains=row.include_subdomains,
                          dns_record=record_out(row), instructions=domain_service.instructions(row))


@router.get("/orgs/{org_id}/domains", response_model=DomainListOut)
async def list_domains(ctx: OrgContext = Depends(require_org_role(Action.VIEW_ORG)),
                       session: AsyncSession = Depends(get_db_session)):
    """Every member sees the domains (never the verify token). Owners and admins also see the DNS record of a pending one."""
    can_manage = permission_rules.permission_for(Action.MANAGE_DOMAINS, ctx.role) is permission_rules.Permission.ALLOWED
    return DomainListOut(items=[domain_out(d, can_manage) for d in await domain_service.list_domains(session, ctx.org_id)])


@router.patch("/orgs/{org_id}/domains/{domain}", response_model=DomainOut)
async def patch_domain(domain: str, body: DomainPatchIn, ctx: OrgContext = Depends(require_org_role(Action.MANAGE_DOMAINS)),
                       factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        row = await domain_service.update_domain(session, ctx, domain, body.auto_join, body.include_subdomains, clock)
    return domain_out(row, True)


@router.delete("/orgs/{org_id}/domains/{domain}", status_code=204)
async def delete_domain(domain: str, ctx: OrgContext = Depends(require_org_role(Action.MANAGE_DOMAINS)),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        await domain_service.remove_domain(session, ctx, domain, clock)
    return Response(status_code=204)


@router.post("/orgs/{org_id}/domains/{domain}/verify", response_model=DomainOut)
async def verify_domain(domain: str, ctx: OrgContext = Depends(require_org_role(Action.MANAGE_DOMAINS)),
                        factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock),
                        redis: Redis = Depends(get_redis_client)):
    """Check the TXT record once, now (10 an hour per domain). Not published yet: 409 VALIDATION_ERROR with the record to add."""
    await hit_fail_open(redis, clock, RATE_DOMAIN_VERIFY, f"{ctx.org_id}:{domain.lower()}")
    async with unit_of_work(factory) as session:
        outcome = await domain_service.verify_now(session, ctx, domain, clock)
    if outcome.verified:
        return domain_out(outcome.domain, True)
    record = outcome.record
    detail = "TXT record not found yet" + (" (the DNS lookup failed, try again shortly)" if outcome.lookup_failed else "")
    return problem_response(None, 409, "VALIDATION_ERROR", detail, extra={
        "domain": outcome.domain.domain, "dns_record": {"type": record.type, "host": record.host, "value": record.value}})


# ---------------------------------------------------------------------------------------------------- the user's own
@router.get("/me/domain-match", response_model=DomainMatchOut)
async def domain_match(user: CurrentUser = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    """For someone with no organisation whose VERIFIED email belongs to a verified domain: the organisation they can ask to
    join. Anything else is 404 (so a user who verified before the domain existed can still find their org)."""
    match = await join_request_service.find_match(session, user.id)
    if match is None:
        raise NotFound("No organisation matches your email address")
    return DomainMatchOut(org_id=match.org_id, org_name=match.org_name, org_type=match.org_type, can_request=True,
                          pending_request_id=match.pending_request_id)


@router.post("/join-requests", status_code=201, response_model=JoinRequestCreatedOut)
async def create_join_request(user: CurrentUser = Depends(get_current_user),
                              factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        request = await join_request_service.create_request(session, user, clock)
    return JoinRequestCreatedOut(id=request.id, org_id=request.org_id, status=request.status)


@router.delete("/join-requests/{request_id}", status_code=204)
async def cancel_join_request(request_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                              factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        await join_request_service.cancel_request(session, user, request_id, clock)
    return Response(status_code=204)


# ---------------------------------------------------------------------------------------------------- the admins'
@router.get("/orgs/{org_id}/join-requests", response_model=JoinRequestPageOut)
async def list_join_requests(status: str | None = "pending", cursor: str | None = None,
                             limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                             ctx: OrgContext = Depends(require_org_role(Action.MANAGE_JOIN_REQUESTS)),
                             session: AsyncSession = Depends(get_db_session)):
    page = await join_request_service.list_requests(session, ctx.org_id, status, cursor, limit)
    return JoinRequestPageOut(items=[JoinRequestRowOut(**vars(row)) for row in page.items], next_cursor=page.next_cursor)


@router.post("/orgs/{org_id}/join-requests/{request_id}/approve", response_model=JoinRequestDecisionOut)
async def approve_join_request(request_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.MANAGE_JOIN_REQUESTS)),
                               factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        request = await join_request_service.approve(session, ctx, request_id, clock)
    return JoinRequestDecisionOut(id=request.id, status=request.status)


@router.post("/orgs/{org_id}/join-requests/{request_id}/decline", response_model=JoinRequestDecisionOut)
async def decline_join_request(request_id: uuid.UUID, ctx: OrgContext = Depends(require_org_role(Action.MANAGE_JOIN_REQUESTS)),
                               factory: async_sessionmaker = Depends(get_session_factory_dep), clock: Clock = Depends(get_clock)):
    async with unit_of_work(factory) as session:
        request = await join_request_service.decline(session, ctx, request_id, clock)
    return JoinRequestDecisionOut(id=request.id, status=request.status)
