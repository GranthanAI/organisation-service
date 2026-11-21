"""One transaction per use case: an exception rolls back everything written in the block."""

import pytest
from sqlalchemy import func, select

from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.repositories.audit_repository import AuditRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.outbox_repository import OutboxRepository
from tests.support.factories import make_audit, make_membership, make_org, make_outbox


class Boom(Exception):
    pass


async def counts(session_factory):
    async with session_factory() as s:
        return tuple([(await s.execute(select(func.count()).select_from(model))).scalar_one()
                      for model in (Membership, OutboxEvent, AuditLog)])


async def seed_org(session_factory):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
    return org


async def test_success_commits_membership_outbox_and_audit_together(session_factory):
    org = await seed_org(session_factory)
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(make_membership(org.id))
        await OutboxRepository(s).add(make_outbox())
        await AuditRepository(s).add(make_audit(org.id, action="member.joined"))
    assert await counts(session_factory) == (1, 1, 1)


async def test_an_exception_rolls_back_the_outbox_and_audit_rows_written_in_the_same_block(session_factory):
    org = await seed_org(session_factory)
    with pytest.raises(Boom):
        async with unit_of_work(session_factory) as s:
            await MembershipRepository(s).add(make_membership(org.id))
            await OutboxRepository(s).add(make_outbox())
            await AuditRepository(s).add(make_audit(org.id, action="member.joined"))
            raise Boom
    assert await counts(session_factory) == (0, 0, 0)


async def test_a_failure_after_the_rows_were_flushed_still_rolls_back(session_factory):
    org = await seed_org(session_factory)
    with pytest.raises(Boom):
        async with unit_of_work(session_factory) as s:
            await OutboxRepository(s).add(make_outbox())
            await s.flush()
            await AuditRepository(s).add(make_audit(org.id))
            await s.flush()
            raise Boom
    assert await counts(session_factory) == (0, 0, 0)


async def test_the_original_exception_is_not_swallowed_or_wrapped(session_factory):
    with pytest.raises(Boom) as exc:
        async with unit_of_work(session_factory):
            raise Boom("original")
    assert str(exc.value) == "original"


async def test_a_constraint_failure_late_in_the_block_undoes_the_earlier_writes(session_factory):
    from app.core.exceptions import AlreadyInOrg
    org = await seed_org(session_factory)
    user_membership = make_membership(org.id)
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(user_membership)
    with pytest.raises(AlreadyInOrg):
        async with unit_of_work(session_factory) as s:
            await OutboxRepository(s).add(make_outbox())
            await AuditRepository(s).add(make_audit(org.id))
            await MembershipRepository(s).add(make_membership(org.id, user_membership.user_id))
    assert await counts(session_factory) == (1, 0, 0)


async def test_repositories_never_commit(session_factory):
    org = await seed_org(session_factory)
    async with session_factory() as s:
        await OutboxRepository(s).add(make_outbox())
        await MembershipRepository(s).add(make_membership(org.id))
        await s.rollback()  # the caller owns the transaction
    assert await counts(session_factory) == (0, 0, 0)


async def test_blocks_are_independent_transactions(session_factory):
    org = await seed_org(session_factory)
    async with unit_of_work(session_factory) as s:
        await OutboxRepository(s).add(make_outbox())
    with pytest.raises(Boom):
        async with unit_of_work(session_factory) as s:
            await AuditRepository(s).add(make_audit(org.id))
            raise Boom
    assert await counts(session_factory) == (0, 1, 0)
