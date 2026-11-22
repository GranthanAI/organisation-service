"""Domain and join-request jobs (Phase 9). Registered in the job registry.

domains.verify (hourly): pending domains added in the last 7 days get their TXT record checked; after 7 days they stay
pending and are not checked automatically (the manual verify still works).
domains.recheck (daily): DNS-verified domains whose last check is older than 30 days are checked again. Found: the failure
count resets. Not found (NXDOMAIN, no answer and timeouts all count): +1, and at 3 the verification is removed and
auto_join turned off. Staff-verified domains are never DNS-rechecked.
join_requests.expire (daily): pending requests older than 30 days become expired.
"""

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock
from app.core.exceptions import DomainTaken
from app.core.logging import logger
from app.models.job import Job
from app.repositories.domain_repository import DomainRepository
from app.services import audit_service, domain_service
from app.services.job_handlers import job_handler

CONCURRENT_LOOKUPS = 10


async def _lookups(rows):
    """DNS answers for many domains at once (a few at a time), before any row is changed."""
    dns = domain_service.get_dns_client()
    gate = asyncio.Semaphore(CONCURRENT_LOOKUPS)

    async def one(row):
        async with gate:
            return await domain_service.check_dns(dns, row)

    return await asyncio.gather(*[one(r) for r in rows])


@job_handler(constants.JOB_DOMAINS_VERIFY)
async def domains_verify(session: AsyncSession, job: Job, clock: Clock) -> None:
    now, repo, verified = clock.now(), None, 0
    repo = DomainRepository(session)
    rows = await repo.pending_to_check(now - timedelta(days=constants.DOMAIN_VERIFY_WINDOW_DAYS), constants.DOMAIN_JOB_BATCH)
    for row, (found, _error) in zip(rows, await _lookups(rows), strict=True):
        row.last_checked_at = now
        if not found:
            continue
        try:
            await domain_service.refuse_if_taken(repo, row.domain, include_children=row.include_subdomains, exclude_org=row.org_id)
            await domain_service.mark_verified(session, row, "dns", None, now)
        except DomainTaken:
            logger.info("domain verification skipped: already verified by an organisation", domain=row.domain)
            continue
        await audit_service.record(session, "system", constants.AUDIT_DOMAIN_VERIFIED, "domain", row.domain,
                                   {"domain": row.domain, "method": "dns"}, org_id=row.org_id, clock=clock)
        verified += 1
    if rows:
        logger.info("pending domains checked", checked=len(rows), verified=verified)


@job_handler(constants.JOB_DOMAINS_RECHECK)
async def domains_recheck(session: AsyncSession, job: Job, clock: Clock) -> None:
    now, repo = clock.now(), DomainRepository(session)
    rows = await repo.dns_verified_due(domain_service.recheck_due_before(now), constants.DOMAIN_JOB_BATCH)
    removed = 0
    for row, (found, _error) in zip(rows, await _lookups(rows), strict=True):
        row.last_checked_at = now
        if found:
            row.failed_checks = 0
            continue
        row.failed_checks += 1
        if row.failed_checks >= constants.DOMAIN_RECHECK_FAILURES:
            row.verified_at = row.verified_by = row.verify_method = None
            row.auto_join, row.failed_checks = False, 0
            removed += 1
            await audit_service.record(
                session, "system", constants.AUDIT_DOMAIN_UNVERIFIED, "domain", row.domain,
                {"domain": row.domain, "reason": "the TXT record was missing on 3 re-checks"}, org_id=row.org_id, clock=clock)
    await session.flush()
    if rows:
        logger.info("verified domains rechecked", checked=len(rows), unverified=removed)


@job_handler(constants.JOB_JOIN_REQUESTS_EXPIRE)
async def join_requests_expire(session: AsyncSession, job: Job, clock: Clock) -> None:
    now, repo, total = clock.now(), DomainRepository(session), 0
    cutoff = now - timedelta(days=constants.JOIN_REQUEST_EXPIRE_DAYS)
    while True:
        expired = await repo.expire_pending_before(cutoff, now, constants.DOMAIN_JOB_BATCH)
        total += expired
        if expired < constants.DOMAIN_JOB_BATCH:
            break
    if total:
        logger.info("join requests expired", count=total)
