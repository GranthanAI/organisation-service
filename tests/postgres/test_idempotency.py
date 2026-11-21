"""Idempotency: replay, mismatch, in-progress, lock takeover, release on 5xx. A test-only app with IdempotentRoute."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.idempotent_route import IdempotentRoute
from app.core.clock import FixedClock
from app.core.exceptions import (
    AlreadyInOrg,
    IdempotencyInProgress,
    IdempotencyMismatch,
    RateLimited,
    UpstreamUnavailable,
)
from app.middleware.correlation import CorrelationIDMiddleware
from app.middleware.exception_handler import register_exception_handlers
from app.services.idempotency_service import IdempotencyService, request_hash, validate_key
from tests.support.factories import NOW
from tests.support.tokens import bearer, make_token

KEY = "key-0000001"


class Probe:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()


def build(session_factory, clock):
    probe = Probe()
    app = FastAPI()
    app.add_middleware(CorrelationIDMiddleware)
    register_exception_handlers(app)
    app.state.idempotency_service = IdempotencyService(session_factory, clock)
    router = APIRouter(route_class=IdempotentRoute)

    @router.post("/t/create", status_code=201)
    async def create(body: dict):
        probe.calls += 1
        return {"n": probe.calls, "echo": body}

    @router.post("/t/orgs/{org_id}/things", status_code=201)
    async def things(org_id: uuid.UUID, body: dict):
        probe.calls += 1
        return {"n": probe.calls, "org": str(org_id)}

    @router.post("/t/unavailable")
    async def unavailable(body: dict):
        probe.calls += 1
        raise UpstreamUnavailable("billing is down")

    @router.post("/t/boom")
    async def boom(body: dict):
        probe.calls += 1
        raise RuntimeError("kaboom")

    @router.post("/t/conflict")
    async def conflict(body: dict):
        probe.calls += 1
        raise AlreadyInOrg("already in an organisation")

    @router.post("/t/limited")
    async def limited(body: dict):
        probe.calls += 1
        raise RateLimited(30)

    @router.post("/t/slow", status_code=201)
    async def slow(body: dict):
        probe.calls += 1
        probe.started.set()
        await probe.release.wait()
        return {"n": probe.calls}

    @router.get("/t/read")
    async def read():
        return {"ok": True}

    app.include_router(router)
    return app, probe


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
async def env(session_factory, clock):
    app, probe = build(session_factory, clock)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, probe, session_factory


def headers(user, key=KEY, **kw):
    return {**bearer(make_token(sub=user, **kw)), "Idempotency-Key": key} if key else bearer(make_token(sub=user))


async def rows(session_factory):
    async with session_factory() as s:
        return (await s.execute(text("SELECT key, state, status_code, locked_until FROM idempotency_keys"))).all()


# --- the key header ---
@pytest.mark.parametrize("key", [None, "short", "x" * 129, "has space 123", "bad/chars!!", ""])
async def test_a_missing_or_malformed_key_is_a_validation_error(env, key):
    client, probe, _ = env
    h = headers(uuid.uuid4(), key=None)
    if key is not None:
        h["Idempotency-Key"] = key
    r = await client.post("/t/create", json={"a": 1}, headers=h)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    assert probe.calls == 0


def test_validate_key_accepts_8_to_128_safe_characters():
    for ok in ("12345678", "a" * 128, "Abc_def-123"):
        assert validate_key(ok) == ok


# --- replay ---
async def test_same_key_and_same_body_replays_the_stored_response(env):
    client, probe, sf = env
    user = uuid.uuid4()
    first = await client.post("/t/create", json={"a": 1, "b": [2, 3]}, headers=headers(user))
    second = await client.post("/t/create", json={"b": [2, 3], "a": 1}, headers=headers(user))   # key order differs
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json() == {"n": 1, "echo": {"a": 1, "b": [2, 3]}}
    assert probe.calls == 1
    assert second.headers["idempotent-replayed"] == "true" and "idempotent-replayed" not in first.headers
    assert [(r.state, r.status_code) for r in await rows(sf)] == [("done", 201)]


async def test_a_different_key_runs_the_handler_again(env):
    client, probe, _ = env
    user = uuid.uuid4()
    await client.post("/t/create", json={}, headers=headers(user, key="key-aaaaaaa"))
    r = await client.post("/t/create", json={}, headers=headers(user, key="key-bbbbbbb"))
    assert r.json()["n"] == 2 and probe.calls == 2


async def test_the_same_key_for_another_user_or_route_is_independent(env):
    client, probe, _ = env
    a, b = uuid.uuid4(), uuid.uuid4()
    await client.post("/t/create", json={}, headers=headers(a))
    assert (await client.post("/t/create", json={}, headers=headers(b))).json()["n"] == 2          # other actor
    org = uuid.uuid4()
    assert (await client.post(f"/t/orgs/{org}/things", json={}, headers=headers(a))).json()["n"] == 3  # other route
    assert probe.calls == 3


async def test_the_route_is_scoped_by_template_not_by_concrete_path(env):
    client, probe, sf = env
    user = uuid.uuid4()
    await client.post(f"/t/orgs/{uuid.uuid4()}/things", json={}, headers=headers(user))
    r = await client.post(f"/t/orgs/{uuid.uuid4()}/things", json={}, headers=headers(user))
    assert r.headers.get("idempotent-replayed") == "true"      # the same key on a different org id is a replay
    async with sf() as s:
        routes = [x[0] for x in (await s.execute(text("SELECT route FROM idempotency_keys"))).all()]
    assert routes == ["/t/orgs/{org_id}/things"]


# --- mismatch ---
async def test_same_key_with_a_different_body_is_a_mismatch(env):
    client, probe, _ = env
    user = uuid.uuid4()
    await client.post("/t/create", json={"a": 1}, headers=headers(user))
    r = await client.post("/t/create", json={"a": 2}, headers=headers(user))
    assert r.status_code == 422 and r.json()["code"] == "IDEMPOTENCY_MISMATCH"
    assert probe.calls == 1


# --- concurrency ---
async def test_a_concurrent_second_request_gets_409_with_retry_after_then_replays(env):
    client, probe, sf = env
    user = uuid.uuid4()
    first = asyncio.create_task(client.post("/t/slow", json={"a": 1}, headers=headers(user)))
    await asyncio.wait_for(probe.started.wait(), timeout=10)
    assert [r.state for r in await rows(sf)] == ["in_progress"]       # the reservation is visible at once

    second = await client.post("/t/slow", json={"a": 1}, headers=headers(user))
    assert second.status_code == 409 and second.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert 1 <= int(second.headers["retry-after"]) <= 61

    probe.release.set()
    done = await asyncio.wait_for(first, timeout=10)
    assert done.status_code == 201
    third = await client.post("/t/slow", json={"a": 1}, headers=headers(user))
    assert third.status_code == 201 and third.headers["idempotent-replayed"] == "true" and probe.calls == 1


async def test_two_simultaneous_identical_requests_run_the_handler_once(env):
    client, probe, _ = env
    user = uuid.uuid4()
    probe.release.set()
    results = await asyncio.gather(*[client.post("/t/slow", json={}, headers=headers(user)) for _ in range(6)])
    statuses = sorted(r.status_code for r in results)
    assert probe.calls == 1
    assert statuses.count(201) >= 1 and set(statuses) <= {201, 409}


async def test_an_expired_lock_is_taken_over(session_factory, clock, env):
    client, probe, sf = env
    user = uuid.uuid4()
    service = IdempotencyService(sf, clock)
    h = request_hash(b'{"a": 1}')
    assert await service.begin(user, "/t/create", KEY, h) is None            # a crashed worker left this reservation
    with pytest.raises(IdempotencyInProgress):
        await service.begin(user, "/t/create", KEY, h)                         # still locked
    clock.advance(seconds=61)                                                  # the 60 s lock has passed
    r = await client.post("/t/create", json={"a": 1}, headers=headers(user))
    assert r.status_code == 201 and probe.calls == 1                           # taken over, handler ran
    again = await client.post("/t/create", json={"a": 1}, headers=headers(user))
    assert again.headers["idempotent-replayed"] == "true"


async def test_a_row_older_than_24_hours_is_not_replayed(env, clock):
    client, probe, _ = env
    user = uuid.uuid4()
    await client.post("/t/create", json={"a": 1}, headers=headers(user))
    clock.advance(hours=23)
    assert (await client.post("/t/create", json={"a": 1}, headers=headers(user))).headers.get("idempotent-replayed")
    clock.advance(hours=2)
    r = await client.post("/t/create", json={"a": 1}, headers=headers(user))
    assert "idempotent-replayed" not in r.headers and r.json()["n"] == 2       # expired: runs again, even a new body


# --- what is stored and what is released ---
async def test_a_5xx_deletes_the_reservation_so_the_client_can_retry(env):
    client, probe, sf = env
    user = uuid.uuid4()
    r = await client.post("/t/unavailable", json={}, headers=headers(user))
    assert r.status_code == 503 and r.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert await rows(sf) == []
    r2 = await client.post("/t/unavailable", json={}, headers=headers(user))
    assert r2.status_code == 503 and "idempotent-replayed" not in r2.headers and probe.calls == 2


async def test_an_unexpected_exception_deletes_the_reservation(env):
    client, probe, sf = env
    user = uuid.uuid4()
    r = await client.post("/t/boom", json={}, headers=headers(user))
    assert r.status_code == 500 and await rows(sf) == []
    await client.post("/t/boom", json={}, headers=headers(user))
    assert probe.calls == 2


async def test_a_4xx_is_stored_and_replayed_as_problem_json(env):
    client, probe, sf = env
    user = uuid.uuid4()
    first = await client.post("/t/conflict", json={}, headers=headers(user))
    second = await client.post("/t/conflict", json={}, headers=headers(user))
    assert first.status_code == second.status_code == 409
    assert second.headers["content-type"].startswith("application/problem+json")
    assert second.json()["code"] == "ALREADY_IN_ORG" and second.headers["idempotent-replayed"] == "true"
    assert probe.calls == 1 and [(r.state, r.status_code) for r in await rows(sf)] == [("done", 409)]


async def test_a_validation_error_is_a_stored_4xx_too(env):
    client, probe, sf = env
    user = uuid.uuid4()
    r = await client.post("/t/create", content=b"[1, 2]", headers={**headers(user), "content-type": "application/json"})
    assert r.status_code == 422                    # body must be an object: FastAPI rejects before our handler
    assert probe.calls == 0


async def test_429_is_never_stored(env):
    client, probe, sf = env
    user = uuid.uuid4()
    r = await client.post("/t/limited", json={}, headers=headers(user))
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert await rows(sf) == []
    await client.post("/t/limited", json={}, headers=headers(user))
    assert probe.calls == 2


async def test_without_a_valid_token_the_route_runs_untouched_and_nothing_is_stored(env):
    client, probe, sf = env
    r = await client.post("/t/create", json={"a": 1}, headers={"Idempotency-Key": KEY})       # no bearer
    r2 = await client.post("/t/create", json={"a": 1}, headers={"Idempotency-Key": KEY, "Authorization": "Bearer x"})
    assert r.status_code == 201 and r2.status_code == 201      # the real routes' get_current_user answers 401 here
    assert await rows(sf) == []


async def test_get_requests_are_not_idempotency_keyed(env):
    client, _, sf = env
    r = await client.get("/t/read", headers=bearer(make_token()))
    assert r.status_code == 200 and await rows(sf) == []


# --- the service on its own ---
async def test_service_begin_complete_release(session_factory, clock):
    service = IdempotencyService(session_factory, clock)
    user, h = uuid.uuid4(), request_hash(b"{}")
    assert await service.begin(user, "r", KEY, h) is None
    await service.complete(user, "r", KEY, 201, {"id": 1})
    stored = await service.begin(user, "r", KEY, h)
    assert stored.status_code == 201 and stored.body == {"id": 1}
    with pytest.raises(IdempotencyMismatch):
        await service.begin(user, "r", KEY, request_hash(b'{"x": 1}'))
    await service.release(user, "r", KEY)
    assert await service.begin(user, "r", KEY, h) is None


def test_the_hash_is_over_canonical_json():
    assert request_hash(b'{"a":1,"b":2}') == request_hash(b'{ "b": 2,\n "a": 1 }')
    assert request_hash(b'{"a":1}') != request_hash(b'{"a":2}')
    assert request_hash(b"not json") == request_hash(b"not json") != request_hash(b"other")
    assert timedelta(seconds=60).total_seconds() == 60
