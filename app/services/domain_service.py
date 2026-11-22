"""Email domains (Phase 9): add, list, change, remove, verify (DNS TXT or staff).

A domain is verified for ONE organisation (`ux_org_domains_verified`), and verified domains never overlap: not the same
domain, not a verified parent that includes subdomains, and (when this domain includes subdomains) not a verified
child. Everything runs in the caller's transaction. The verification token is public by design (it is published in DNS)
and is never written to the audit log.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.dns_client import DnsClient, get_dns_client
from app.core import constants
from app.core.clock import Clock, system_clock
from app.core.exceptions import DomainTaken, NotFound, OrgInactive, ValidationFailed
from app.db.errors import map_integrity_error
from app.models.domain import OrgDomain
from app.repositories.domain_repository import DomainRepository
from app.repositories.org_repository import OrgRepository
from app.security.models import CurrentUser, OrgContext
from app.services import audit_service, domain_rules, org_gate
from app.services.invite_service import ACCEPTING
from app.telemetry.metrics import DOMAIN_CHECKS


@dataclass(frozen=True)
class DnsRecord:
    type: str
    host: str
    value: str


@dataclass(frozen=True)
class VerifyOutcome:
    verified: bool
    domain: OrgDomain
    record: DnsRecord | None = None         # what the owner still has to publish (when not verified)
    lookup_failed: bool = False


def dns_record(domain: OrgDomain) -> DnsRecord:
    return DnsRecord("TXT", "@", domain_rules.txt_value(domain.verify_token))


def instructions(domain: OrgDomain) -> str:
    record = dns_record(domain)
    return (f"Add a TXT record to {domain.domain} (host {record.host}, the domain root) with the value {record.value}, "
            "then verify. DNS changes can take a few minutes, sometimes hours, to show up.")


def status_of(domain: OrgDomain) -> str:
    return "verified" if domain.verified_at is not None else "pending"


async def _require_accepting(session: AsyncSession, org_id: uuid.UUID) -> None:
    org = await OrgRepository(session).get(org_id)
    if org is None:
        raise NotFound("Organisation not found")
    if org.status not in ACCEPTING:
        raise OrgInactive("this organisation cannot change its domains right now")


async def refuse_if_taken(repo: DomainRepository, domain: str, *, include_children: bool,
                           exclude_org: uuid.UUID | None) -> None:
    if await repo.verified_blocking(domain, include_children=include_children, exclude_org=exclude_org):
        raise DomainTaken("this domain is already verified by an organisation")        # never says which


# ---------------------------------------------------------------------------------------------------- add, list, change, remove
async def add_domain(session: AsyncSession, ctx: OrgContext, raw: str, include_subdomains: bool = False,
                     clock: Clock = system_clock) -> OrgDomain:
    await _require_accepting(session, ctx.org_id)
    domain = domain_rules.check_new_domain(raw)                  # ValidationFailed, or PublicEmailDomain for gmail and kin
    repo = DomainRepository(session)
    if await repo.get(ctx.org_id, domain) is not None:
        raise ValidationFailed("this domain is already added")
    # Another org having only ADDED it is fine; verified by any org (itself, a parent with subdomains, or a child) is not.
    await refuse_if_taken(repo, domain, include_children=True, exclude_org=None)
    if await repo.count_for_org(ctx.org_id) >= domain_rules.MAX_DOMAINS_PER_ORG:
        raise ValidationFailed(f"an organisation can have at most {domain_rules.MAX_DOMAINS_PER_ORG} domains")
    row = OrgDomain(org_id=ctx.org_id, domain=domain, verify_token=secrets.token_urlsafe(32), auto_join=False,
                    include_subdomains=include_subdomains, failed_checks=0, added_at=clock.now())
    await repo.add(row)
    await audit_service.record(session, ctx.user, constants.AUDIT_DOMAIN_ADDED, "domain", domain,
                               {"domain": domain, "include_subdomains": include_subdomains}, org_id=ctx.org_id, clock=clock)
    return row


async def list_domains(session: AsyncSession, org_id: uuid.UUID) -> list[OrgDomain]:
    return await DomainRepository(session).list_for_org(org_id)


async def update_domain(session: AsyncSession, ctx: OrgContext, raw: str, auto_join: bool | None,
                        include_subdomains: bool | None, clock: Clock = system_clock) -> OrgDomain:
    """auto_join needs the domain verified; changing include_subdomains on a verified domain re-checks the ownership rule."""
    await _require_accepting(session, ctx.org_id)
    repo = DomainRepository(session)
    row = await repo.get_for_update(ctx.org_id, domain_rules.check_domain_name(raw))
    if row is None:
        raise NotFound("Domain not found")
    changes: dict[str, bool] = {}
    if include_subdomains is not None and include_subdomains != row.include_subdomains:
        if row.verified_at is not None and include_subdomains:
            await refuse_if_taken(repo, row.domain, include_children=True, exclude_org=ctx.org_id)
        row.include_subdomains = include_subdomains
        changes["include_subdomains"] = include_subdomains
    if auto_join is not None and auto_join != row.auto_join:
        if auto_join and row.verified_at is None:
            raise ValidationFailed("verify the domain before turning auto-join on")
        row.auto_join = auto_join
        changes["auto_join"] = auto_join
    if changes:
        await repo.save(row)
        await audit_service.record(session, ctx.user, constants.AUDIT_DOMAIN_UPDATED, "domain", row.domain,
                                   {"domain": row.domain, **changes}, org_id=ctx.org_id, clock=clock)
    return row


async def remove_domain(session: AsyncSession, ctx: OrgContext, raw: str, clock: Clock = system_clock) -> None:
    """Remove a domain. Members who joined through it stay."""
    await org_gate.require_open(session, ctx.org_id)
    repo = DomainRepository(session)
    row = await repo.get_for_update(ctx.org_id, domain_rules.check_domain_name(raw))
    if row is None:
        raise NotFound("Domain not found")
    await repo.delete(row)
    await audit_service.record(session, ctx.user, constants.AUDIT_DOMAIN_REMOVED, "domain", row.domain,
                               {"domain": row.domain, "was_verified": row.verified_at is not None}, org_id=ctx.org_id, clock=clock)


# ---------------------------------------------------------------------------------------------------- verify
async def check_dns(dns: DnsClient, domain: OrgDomain) -> tuple[bool, bool]:
    """(found, error): is the expected TXT record published at the domain root? Counts org_domain_checks_total."""
    lookup = await dns.txt(domain.domain)
    found = domain_rules.txt_matches(lookup.records, domain.verify_token)
    DOMAIN_CHECKS.labels(result="found" if found else ("error" if lookup.error else "not_found")).inc()
    return found, lookup.error


async def mark_verified(session: AsyncSession, row: OrgDomain, method: str, by: uuid.UUID | None, now: datetime) -> None:
    """Set the verification; the unique index losing a race (another org verified it first) is DOMAIN_TAKEN."""
    row.verified_at, row.verified_by, row.verify_method = now, by, method
    row.failed_checks, row.last_checked_at = 0, now
    try:
        async with session.begin_nested():
            await DomainRepository(session).save(row)
    except IntegrityError as exc:
        mapped = map_integrity_error(exc)
        row.verified_at, row.verified_by, row.verify_method = None, None, None
        if mapped is None:
            raise
        raise DomainTaken("this domain is already verified by an organisation") from exc


async def verify_now(session: AsyncSession, ctx: OrgContext, raw: str, clock: Clock = system_clock,
                     dns: DnsClient | None = None) -> VerifyOutcome:
    """Check the TXT record once, now. Found: verified. Not found: nothing changes (the outcome carries the record to publish)."""
    await _require_accepting(session, ctx.org_id)
    repo = DomainRepository(session)
    row = await repo.get_for_update(ctx.org_id, domain_rules.check_domain_name(raw))
    if row is None:
        raise NotFound("Domain not found")
    if row.verified_at is not None:
        return VerifyOutcome(True, row)
    found, error = await check_dns(dns or get_dns_client(), row)
    if not found:
        return VerifyOutcome(False, row, dns_record(row), lookup_failed=error)
    await refuse_if_taken(repo, row.domain, include_children=row.include_subdomains, exclude_org=ctx.org_id)
    await mark_verified(session, row, "dns", ctx.user.id, clock.now())
    await audit_service.record(session, ctx.user, constants.AUDIT_DOMAIN_VERIFIED, "domain", row.domain,
                               {"domain": row.domain, "method": "dns"}, org_id=ctx.org_id, clock=clock)
    return VerifyOutcome(True, row)


async def manual_verify(session: AsyncSession, actor: CurrentUser, org_id: uuid.UUID, raw: str, reason: str,
                        clock: Clock = system_clock) -> OrgDomain:
    """Staff verify a domain by hand (a college whose IT is slow). The same ownership rules; never DNS-rechecked."""
    repo = DomainRepository(session)
    row = await repo.get_for_update(org_id, domain_rules.check_domain_name(raw))
    if row is None:
        raise NotFound("Domain not found")
    if row.verified_at is None:
        await refuse_if_taken(repo, row.domain, include_children=row.include_subdomains, exclude_org=org_id)
        await mark_verified(session, row, "staff", actor.id, clock.now())
        await audit_service.record(session, actor, constants.AUDIT_DOMAIN_VERIFIED, "domain", row.domain,
                                   {"domain": row.domain, "method": "staff"}, reason, org_id=org_id, staff=True, clock=clock)
    return row


def recheck_due_before(now: datetime) -> datetime:
    return now - timedelta(days=constants.DOMAIN_RECHECK_DAYS)
