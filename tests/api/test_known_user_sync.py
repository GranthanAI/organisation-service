"""known_users is filled from logged-in requests (K2): created, updated, deleted users untouched, cached, never fatal."""

import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_db_session, get_redis_client
from app.db.session import unit_of_work
from app.middleware.exception_handler import register_exception_handlers
from app.models.known_user import KnownUser
from app.repositories import known_user_repository as repo_module
from app.repositories.known_user_repository import KnownUserRepository
from app.security import known_user_sync
from app.security.dependencies import get_current_user
from app.security.models import CurrentUser
from tests.support.tokens import bearer, make_token

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def build_app(session_factory) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    async def session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_redis_client] = lambda: fakeredis.FakeAsyncRedis()

    @app.get("/t/me")
    async def me(user: CurrentUser = Depends(get_current_user)):
        return {"id": str(user.id)}

    return app


@pytest.fixture
async def client(session_factory):
    async with AsyncClient(transport=ASGITransport(app=build_app(session_factory)), base_url="http://t") as c:
        yield c


async def row(session_factory, user) -> KnownUser | None:
    async with session_factory() as s:
        return await s.get(KnownUser, user)


@pytest.fixture
def writes(monkeypatch):
    """Counts calls to the repository's sync_email (each one is a database write)."""
    calls = []
    real = repo_module.KnownUserRepository.sync_email

    async def counting(self, *args, **kwargs):
        calls.append(args)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(repo_module.KnownUserRepository, "sync_email", counting)
    return calls


async def test_a_missing_row_is_created_from_the_token(client, session_factory):
    user = uuid.uuid4()
    r = await client.get("/t/me", headers=bearer(make_token(sub=user, email="New.Student@Amity.edu")))
    assert r.status_code == 200
    created = await row(session_factory, user)
    assert created.email == "new.student@amity.edu"
    assert created.full_name is None and created.email_verified_at is None and created.deleted_at is None


async def test_a_changed_email_is_updated_and_nothing_else(client, session_factory):
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).upsert(user, "old@amity.edu", "Old Name", NOW)
        await KnownUserRepository(s).set_email_verified(user, "old@amity.edu", NOW, NOW)
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="renamed@amity.edu")))
    updated = await row(session_factory, user)
    assert updated.email == "renamed@amity.edu"
    assert updated.full_name == "Old Name" and updated.email_verified_at == NOW      # never set or cleared from a JWT


async def test_a_deleted_user_is_untouched_and_gets_401(client, session_factory):
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).upsert(user, "gone@amity.edu", "Gone", NOW)
        await KnownUserRepository(s).set_deleted(user, NOW)
    r = await client.get("/t/me", headers=bearer(make_token(sub=user, email="other@amity.edu")))
    assert r.status_code == 401
    after = await row(session_factory, user)
    assert (after.email, after.deleted_at, after.updated_at) == ("gone@amity.edu", NOW, NOW)


async def test_the_repository_never_updates_a_deleted_row_even_if_called_directly(session_factory):
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).upsert(user, "gone@amity.edu", None, NOW)
        await KnownUserRepository(s).set_deleted(user, NOW)
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).sync_email(user, "other@amity.edu", NOW)
    assert (await row(session_factory, user)).email == "gone@amity.edu"


async def test_a_second_request_does_no_write(client, session_factory, writes):
    user = uuid.uuid4()
    token = make_token(sub=user, email="cached@amity.edu")
    await client.get("/t/me", headers=bearer(token))
    await client.get("/t/me", headers=bearer(token))
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="cached@amity.edu")))     # another token, same user
    assert len(writes) == 1


async def test_an_in_sync_row_is_not_written_and_is_remembered(client, session_factory, writes):
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).upsert(user, "same@amity.edu", "Same", NOW)
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="same@amity.edu")))
    assert writes == []


async def test_the_cache_expires_after_ten_minutes(client, session_factory, writes, monkeypatch):
    user = uuid.uuid4()
    clock = {"now": 1000.0}
    monkeypatch.setattr(known_user_sync.time, "monotonic", lambda: clock["now"])
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="a@amity.edu")))
    clock["now"] += 599
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="a@amity.edu")))
    assert len(writes) == 1
    # the email changes at the source: the next request after the TTL notices and writes
    clock["now"] += 2
    async with unit_of_work(session_factory) as s:
        await s.execute(KnownUser.__table__.update().where(KnownUser.user_id == user).values(email="drifted@amity.edu"))
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="a@amity.edu")))
    assert len(writes) == 2 and (await row(session_factory, user)).email == "a@amity.edu"


async def test_a_new_email_is_not_hidden_by_the_cache_of_the_old_one(client, session_factory, writes):
    user = uuid.uuid4()
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="first@amity.edu")))
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="second@amity.edu")))
    assert len(writes) == 2 and (await row(session_factory, user)).email == "second@amity.edu"


async def test_a_database_error_in_the_sync_never_fails_the_request(client, session_factory, monkeypatch, caplog):
    async def broken(self, *args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(repo_module.KnownUserRepository, "sync_email", broken)
    user = uuid.uuid4()
    r = await client.get("/t/me", headers=bearer(make_token(sub=user, email="x@amity.edu")))
    assert r.status_code == 200
    assert "could not sync known_users" in caplog.text
    assert await row(session_factory, user) is None
    # and a later request tries again (a failure is not cached as "in sync")
    monkeypatch.undo()
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="x@amity.edu")))
    assert await row(session_factory, user) is not None


async def test_the_sync_does_not_leak_into_the_requests_own_session(client, session_factory):
    user = uuid.uuid4()
    await client.get("/t/me", headers=bearer(make_token(sub=user, email="own@amity.edu")))
    async with session_factory() as s:
        count = len(list((await s.execute(select(KnownUser))).scalars()))
    assert count == 1
