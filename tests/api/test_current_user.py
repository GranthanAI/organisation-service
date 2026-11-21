"""get_current_user on a test-only app: revoked tokens, Redis down, deleted users. Throwaway database + fakeredis."""

import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db_session, get_redis_client
from app.db.session import unit_of_work
from app.middleware.correlation import CorrelationIDMiddleware
from app.middleware.exception_handler import register_exception_handlers
from app.repositories.known_user_repository import KnownUserRepository
from app.security.dependencies import get_current_user
from app.security.models import CurrentUser
from tests.support.tokens import bearer, make_token

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class BrokenRedis:
    async def exists(self, *_a, **_k):
        raise ConnectionError("redis is down")


def build_app(session_factory, redis) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIDMiddleware)
    register_exception_handlers(app)

    async def session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_redis_client] = lambda: redis

    @app.get("/t/me")
    async def me(user: CurrentUser = Depends(get_current_user)):
        return {"id": str(user.id), "email": user.email, "staff": user.staff_role}

    @app.post("/t/write")
    async def write(user: CurrentUser = Depends(get_current_user)):
        return {"ok": True}

    @app.put("/t/put")
    async def put(user: CurrentUser = Depends(get_current_user)):
        return {"ok": True}

    @app.delete("/t/delete")
    async def delete(user: CurrentUser = Depends(get_current_user)):
        return {"ok": True}

    return app


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
async def client(session_factory, redis):
    app = build_app(session_factory, redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_a_valid_token_identifies_the_user(client):
    user = uuid.uuid4()
    r = await client.get("/t/me", headers=bearer(make_token(sub=user, email="Ada@Amity.edu", roles=["support"])))
    assert r.status_code == 200
    assert r.json() == {"id": str(user), "email": "ada@amity.edu", "staff": "support"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer"}, {"Authorization": "Bearer nonsense"},
                                     {"Authorization": "Basic abc"}])
async def test_missing_or_bad_credentials_give_401_problem_json(client, headers):
    r = await client.get("/t/me", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "UNAUTHENTICATED" and body["detail"] == "Authentication required"


async def test_a_blacklisted_jti_gives_401(client, redis):
    token = make_token(jti="revoked-jti-1")
    assert (await client.get("/t/me", headers=bearer(token))).status_code == 200
    await redis.set("blacklist:revoked-jti-1", "1", ex=60)   # what auth-service writes on logout
    r = await client.get("/t/me", headers=bearer(token))
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    assert (await client.post("/t/write", headers=bearer(token))).status_code == 401
    # another token of the same user is unaffected
    assert (await client.get("/t/me", headers=bearer(make_token(jti="other-jti")))).status_code == 200


async def test_redis_down_lets_get_through_but_refuses_writes_with_503(session_factory, caplog):
    app = build_app(session_factory, BrokenRedis())
    token = make_token(jti="jti-while-redis-down")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/t/me", headers=bearer(token))).status_code == 200
        for method, path in (("POST", "/t/write"), ("PUT", "/t/put"), ("DELETE", "/t/delete")):
            r = await c.request(method, path, headers=bearer(token))
            assert r.status_code == 503, method
            assert r.json()["code"] == "UPSTREAM_UNAVAILABLE"
            assert r.headers["content-type"].startswith("application/problem+json")
    logged = caplog.text
    assert "blacklist unavailable" in logged                                # the warning is logged ...
    assert "jti-while-redis-down" not in logged                             # ... without the jti
    assert token not in logged and "Bearer" not in logged


async def test_a_deleted_user_gives_401(client, session_factory):
    user = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        repo = KnownUserRepository(s)
        await repo.upsert(user, "gone@amity.edu", "Gone", NOW)
        await repo.set_deleted(user, NOW)
    r = await client.get("/t/me", headers=bearer(make_token(sub=user)))
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"


async def test_a_known_user_who_is_not_deleted_and_an_unknown_user_both_pass(client, session_factory):
    known = uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        await KnownUserRepository(s).upsert(known, "here@amity.edu", "Here", NOW)
    assert (await client.get("/t/me", headers=bearer(make_token(sub=known)))).status_code == 200
    assert (await client.get("/t/me", headers=bearer(make_token(sub=uuid.uuid4())))).status_code == 200


async def test_the_correlation_id_is_in_the_401_body_and_header(client):
    r = await client.get("/t/me", headers={"X-Correlation-ID": "cid-401"})
    assert r.status_code == 401 and r.json()["correlation_id"] == "cid-401" and r.headers["x-correlation-id"] == "cid-401"
