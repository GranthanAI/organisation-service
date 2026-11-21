"""audit_log is insert-only: UPDATE and DELETE both fail, however they are issued."""

import inspect

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError

from app.models.audit_log import AuditLog
from app.repositories.audit_repository import AuditRepository
from tests.support.factories import NOW, make_audit, make_org


async def seed(session_factory):
    org = make_org()
    async with session_factory() as s:
        s.add(org)
        await s.flush()
        entry = await AuditRepository(s).add(make_audit(org.id, reason="because"))
        await s.commit()
    return org, entry


async def test_insert_works_and_reads_back(session_factory):
    org, entry = await seed(session_factory)
    async with session_factory() as s:
        rows = await AuditRepository(s).list_page(org.id)
    assert [r.id for r in rows] == [entry.id]
    assert rows[0].details == {"a": 1} and rows[0].reason == "because"


async def test_raw_update_fails_and_the_row_is_unchanged(session_factory):
    org, entry = await seed(session_factory)
    async with session_factory() as s:
        with pytest.raises(DBAPIError, match="insert-only"):
            await s.execute(text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"), {"id": entry.id})
    async with session_factory() as s:
        assert (await s.get(AuditLog, entry.id)).action == "org.created"


async def test_raw_delete_fails_and_the_row_remains(session_factory):
    org, entry = await seed(session_factory)
    async with session_factory() as s:
        with pytest.raises(DBAPIError, match="insert-only"):
            await s.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})
    async with session_factory() as s:
        assert await s.get(AuditLog, entry.id) is not None


async def test_orm_update_and_delete_fail(session_factory):
    org, entry = await seed(session_factory)
    async with session_factory() as s:
        with pytest.raises(DBAPIError, match="insert-only"):
            await s.execute(update(AuditLog).where(AuditLog.id == entry.id).values(action="x"))
    async with session_factory() as s:
        with pytest.raises(DBAPIError, match="insert-only"):
            await s.execute(delete(AuditLog))
    async with session_factory() as s:
        loaded = await s.get(AuditLog, entry.id)
        loaded.reason = "changed"
        with pytest.raises(DBAPIError, match="insert-only"):
            await s.flush()


async def test_mass_update_touching_every_row_fails(session_factory):
    org, _ = await seed(session_factory)
    async with session_factory() as s:
        await AuditRepository(s).add(make_audit(org.id, action="member.joined"))
        await s.commit()
    async with session_factory() as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE audit_log SET reason = NULL"))


async def test_the_error_is_a_restrict_violation(session_factory):
    org, entry = await seed(session_factory)
    async with session_factory() as s:
        with pytest.raises(DBAPIError) as exc:
            await s.execute(text("DELETE FROM audit_log"))
    assert getattr(exc.value.orig, "sqlstate", None) == "23001"  # restrict_violation


def test_the_repository_has_no_update_or_delete_methods():
    public = {n for n, _ in inspect.getmembers(AuditRepository, inspect.isfunction) if not n.startswith("_")}
    assert public == {"add", "list_page"}


async def test_keyset_paging_newest_first(session_factory):
    from datetime import timedelta
    org = make_org()
    async with session_factory() as s:
        s.add(org)
        await s.flush()
        repo = AuditRepository(s)
        for i in range(5):
            await repo.add(make_audit(org.id, action=f"a{i}", at=NOW + timedelta(minutes=i)))
        await s.commit()
    async with session_factory() as s:
        repo = AuditRepository(s)
        first = await repo.list_page(org.id, limit=2)
        second = await repo.list_page(org.id, after=(first[-1].at, first[-1].id), limit=2)
        only = await repo.list_page(org.id, action="a3")
    assert [r.action for r in first] == ["a4", "a3"]
    assert [r.action for r in second] == ["a2", "a1"]
    assert [r.action for r in only] == ["a3"]
