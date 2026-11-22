"""Redis fixed-window rate limits with fakeredis: limit, Retry-After, fail-closed vs fail-open, X-Forwarded-For."""

import uuid
from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.api.deps import get_clock, get_redis_client
from app.core.clock import FixedClock
from app.core.config import settings
from app.core.constants import (
    RATE_BUCKETS,
    RATE_CSV_UPLOAD,
    RATE_INVITE_ACCEPT,
    RATE_INVITE_CREATE,
    RATE_INVITE_PREVIEW,
    RateLimitBucket,
)
from app.middleware.correlation import CorrelationIDMiddleware
from app.middleware.exception_handler import register_exception_handlers
from app.middleware.rate_limit import client_ip, rate_limit
from app.security.dependencies import get_current_user
from app.security.models import CurrentUser

# 12:00:20 UTC: 20 s into a 60 s window and 20 s into a 3600 s hour (12:00:00 is on an hour boundary)
T0 = datetime(2026, 11, 21, 12, 0, 20, tzinfo=timezone.utc)


class BrokenRedis:
    def pipeline(self, *a, **k):
        raise ConnectionError("redis is down")


def build(redis, clock, buckets: dict[str, object]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIDMiddleware)
    register_exception_handlers(app)
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[get_clock] = lambda: clock

    def fake_user(request: Request) -> CurrentUser:
        return CurrentUser(id=uuid.UUID(request.headers["x-test-user"]), email="u@amity.edu", jti="j")

    app.dependency_overrides[get_current_user] = fake_user
    for path, dep in buckets.items():
        app.get(path, dependencies=[Depends(dep)])(lambda: {"ok": True})
    return app


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def clock():
    return FixedClock(T0)


def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def user_headers(user=None):
    return {"x-test-user": str(user or uuid.UUID(int=1))}


# --- the limit ---
async def test_the_limit_is_hit_with_429_and_retry_after(redis, clock):
    app = build(redis, clock, {"/x": rate_limit("t", 3, 60, "user", False)})
    async with client_for(app) as c:
        for _ in range(3):
            assert (await c.get("/x", headers=user_headers())).status_code == 200
        r = await c.get("/x", headers=user_headers())
    assert r.status_code == 429
    assert r.json()["code"] == "RATE_LIMITED" and r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["retry-after"] == "40"     # 20 s into the window: 40 s left


async def test_a_new_window_resets_the_count(redis, clock):
    app = build(redis, clock, {"/x": rate_limit("t", 1, 60, "user", False)})
    async with client_for(app) as c:
        assert (await c.get("/x", headers=user_headers())).status_code == 200
        assert (await c.get("/x", headers=user_headers())).status_code == 429
        clock.advance(seconds=41)      # 12:01:01
        assert (await c.get("/x", headers=user_headers())).status_code == 200


async def test_retry_after_counts_down_to_the_window_end(redis, clock):
    app = build(redis, clock, {"/x": rate_limit("t", 1, 3600, "user", False)})
    async with client_for(app) as c:
        await c.get("/x", headers=user_headers())
        assert (await c.get("/x", headers=user_headers())).headers["retry-after"] == str(3600 - 20)
        clock.advance(minutes=30)
        assert (await c.get("/x", headers=user_headers())).headers["retry-after"] == str(3600 - 20 - 1800)


async def test_users_and_buckets_are_counted_separately(redis, clock):
    app = build(redis, clock, {"/a": rate_limit("a", 1, 60, "user", False), "/b": rate_limit("b", 1, 60, "user", False)})
    async with client_for(app) as c:
        u1, u2 = user_headers(uuid.UUID(int=1)), user_headers(uuid.UUID(int=2))
        assert (await c.get("/a", headers=u1)).status_code == 200
        assert (await c.get("/a", headers=u1)).status_code == 429
        assert (await c.get("/a", headers=u2)).status_code == 200     # another user
        assert (await c.get("/b", headers=u1)).status_code == 200     # another bucket


async def test_incr_and_expire_are_set_together(redis, clock):
    app = build(redis, clock, {"/x": rate_limit("t", 5, 60, "user", False)})
    async with client_for(app) as c:
        await c.get("/x", headers=user_headers())
    keys = [k async for k in redis.scan_iter("rl:*")]
    assert len(keys) == 1
    assert 0 < await redis.ttl(keys[0]) <= 61
    assert keys[0].decode().startswith(f"rl:t:{uuid.UUID(int=1)}:")


# --- Redis down ---
async def test_fail_closed_gives_503_when_redis_is_down(clock):
    app = build(BrokenRedis(), clock, {"/x": rate_limit("t", 5, 60, "user", True)})
    async with client_for(app) as c:
        r = await c.get("/x", headers=user_headers())
    assert r.status_code == 503 and r.json()["code"] == "UPSTREAM_UNAVAILABLE"


async def test_fail_open_lets_the_request_through_when_redis_is_down(clock, caplog):
    app = build(BrokenRedis(), clock, {"/x": rate_limit("t", 5, 60, "user", False)})
    async with client_for(app) as c:
        r = await c.get("/x", headers=user_headers())
    assert r.status_code == 200
    assert "rate limit store unavailable" in caplog.text


# --- buckets ---
def test_the_buckets_are_as_specified():
    assert RATE_INVITE_CREATE == RateLimitBucket("invite_create", 30, 60, "user", True)
    assert RATE_CSV_UPLOAD == RateLimitBucket("csv_upload", 5, 3600, "user", True)
    assert RATE_INVITE_PREVIEW == RateLimitBucket("invite_preview", 30, 60, "ip", False)
    assert RATE_INVITE_ACCEPT == RateLimitBucket("invite_accept", 10, 60, "user", False)
    assert set(RATE_BUCKETS) == {"invite_create", "csv_upload", "invite_preview", "invite_accept", "domain_verify"}


async def test_a_bucket_object_drives_the_dependency(redis, clock):
    app = build(redis, clock, {"/accept": rate_limit(RATE_INVITE_ACCEPT)})
    async with client_for(app) as c:
        codes = [(await c.get("/accept", headers=user_headers())).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]


def test_rate_limit_needs_all_its_arguments():
    with pytest.raises(ValueError):
        rate_limit("x", 5, 60)
    with pytest.raises(ValueError):
        rate_limit("x", 5, 60, "tenant", True)


# --- client IP / X-Forwarded-For ---
def fake_request(peer: str | None, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 5000) if peer else None})


@pytest.mark.parametrize("peer, xff, allow, expected", [
    ("127.0.0.1", "203.0.113.9", "127.0.0.1", "203.0.113.9"),                  # trusted proxy: believe it
    ("203.0.113.50", "198.51.100.1", "127.0.0.1", "203.0.113.50"),             # untrusted peer: ignore the header
    ("127.0.0.1", None, "127.0.0.1", "127.0.0.1"),                             # no header: the peer
    ("127.0.0.1", "6.6.6.6, 203.0.113.9", "127.0.0.1", "203.0.113.9"),         # a client-supplied prefix is skipped
    ("10.0.0.2", "203.0.113.9, 10.0.0.7", "10.0.0.0/8", "203.0.113.9"),        # trusted hops on the right are skipped
    ("10.0.0.2", "10.0.0.7, 10.0.0.8", "10.0.0.0/8", "10.0.0.7"),              # all trusted: the left-most
    ("8.8.8.8", "203.0.113.9", "*", "203.0.113.9"),                            # trust everyone
    ("127.0.0.1", "203.0.113.9", "", "127.0.0.1"),                             # trust nobody
    ("127.0.0.1", "203.0.113.9", "garbage,127.0.0.1", "203.0.113.9"),          # a bad entry is ignored
    (None, None, "127.0.0.1", "unknown"),
])
def test_client_ip(peer, xff, allow, expected):
    assert client_ip(fake_request(peer, xff), allow_ips=allow) == expected


async def test_per_ip_buckets_use_the_forwarded_address_only_from_trusted_peers(redis, clock, monkeypatch):
    app = build(redis, clock, {"/p": rate_limit(RateLimitBucket("p", 1, 60, "ip", False))})
    async with client_for(app) as c:       # httpx's ASGI client connects from 127.0.0.1
        monkeypatch.setattr(settings, "FORWARDED_ALLOW_IPS", "127.0.0.1")
        assert (await c.get("/p", headers={"x-forwarded-for": "203.0.113.1"})).status_code == 200
        assert (await c.get("/p", headers={"x-forwarded-for": "203.0.113.2"})).status_code == 200   # another client
        assert (await c.get("/p", headers={"x-forwarded-for": "203.0.113.1"})).status_code == 429

        monkeypatch.setattr(settings, "FORWARDED_ALLOW_IPS", "10.0.0.0/8")   # 127.0.0.1 no longer a trusted proxy
        assert (await c.get("/p", headers={"x-forwarded-for": "203.0.113.77"})).status_code == 200  # counted as 127.0.0.1
        assert (await c.get("/p", headers={"x-forwarded-for": "203.0.113.88"})).status_code == 429  # the same peer: spoof fails
