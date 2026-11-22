"""O2: the request size limit. Over the limit is 413 REQUEST_TOO_LARGE (problem+json) before any route code runs."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.idempotency_key import IdempotencyKey
from app.models.invite import Invite
from app.services import csv_service, invite_service

LIMIT = 1000


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", LIMIT)
    monkeypatch.setattr(settings, "CSV_MAX_BYTES", 2000)


@pytest.fixture
def forbidden_routes(monkeypatch):
    """Route code must not run on a 413: any call fails the test."""
    calls = []

    async def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("route code ran")

    monkeypatch.setattr(invite_service, "create_invites", boom)
    monkeypatch.setattr(csv_service, "upload", boom)
    return calls


def invite_url(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/invites"


def headers(api, who):
    return {**api.h(who), "Idempotency-Key": uuid.uuid4().hex}


def emails_body(total_bytes: int) -> bytes:
    """A JSON body of exactly `total_bytes`: one e-mail padded with a long (valid JSON) role string."""
    base = json.dumps({"emails": ["a@amity.edu"], "role": ""}).encode()
    pad = total_bytes - len(base)
    assert pad >= 0
    return json.dumps({"emails": ["a@amity.edu"], "role": "x" * pad}).encode()


async def count(api, model):
    async with api.sf() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


def assert_413(r, limit=LIMIT):
    assert r.status_code == 413
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "REQUEST_TOO_LARGE" and body["status"] == 413 and str(limit) in body["detail"]


async def test_a_normal_route_over_the_limit_is_413_and_under_it_is_served(api, forbidden_routes):
    world = await api.seed_org()
    over = await api.client.post(invite_url(world), content=emails_body(LIMIT + 1),
                                 headers={**headers(api, world.owner), "Content-Type": "application/json"})
    assert_413(over)
    assert forbidden_routes == [] and await count(api, IdempotencyKey) == 0          # no route code, no idempotency reservation
    exactly = await api.client.post(invite_url(world), content=emails_body(LIMIT),
                                    headers={**headers(api, world.owner), "Content-Type": "application/json"})
    assert exactly.status_code != 413                                                 # at the limit is allowed (here: reaches the route)


async def test_get_and_small_bodies_are_untouched(api):
    world = await api.seed_org()
    assert (await api.client.get(f"/org/v1/orgs/{world.org.id}/invites", headers=api.h(world.owner))).status_code == 200
    assert (await api.post(invite_url(world), {"emails": ["a@amity.edu"]}, api.h(world.owner))).status_code == 200


async def test_a_413_carries_the_correlation_id(api):
    world = await api.seed_org()
    r = await api.client.post(invite_url(world), content=b"x" * (LIMIT + 1),
                              headers={**headers(api, world.owner), "X-Correlation-ID": "trace-413"})
    assert_413(r)
    assert r.headers["x-correlation-id"] == "trace-413" and r.json()["correlation_id"] == "trace-413"


async def test_a_chunked_body_over_the_limit_is_cut_off_with_413(api, forbidden_routes):
    world = await api.seed_org()

    async def chunks(n, size):
        for _ in range(n):
            yield b"y" * size                       # no Content-Length: the bytes are counted as they stream

    over = await api.client.post(invite_url(world), content=chunks(10, 300),
                                 headers={**headers(api, world.owner), "Content-Type": "application/json"})
    assert_413(over)
    assert forbidden_routes == [] and await count(api, IdempotencyKey) == 0


async def test_a_chunked_body_under_the_limit_is_served(api):
    world = await api.seed_org()

    async def small():
        yield json.dumps({"emails": ["a@amity.edu"]}).encode()

    ok = await api.client.post(invite_url(world), content=small(), headers={**headers(api, world.owner), "Content-Type": "application/json"})
    assert ok.status_code == 200


async def test_a_chunked_body_exactly_at_the_limit_passes(api):
    world = await api.seed_org()

    async def chunks():
        yield b"z" * 500
        yield b"z" * 500

    r = await api.client.post(invite_url(world), content=chunks(),
                              headers={**headers(api, world.owner), "Content-Type": "application/json"})
    assert r.status_code == 422                                                          # reached validation: not 413


# ---- the CSV route: CSV_MAX_BYTES + 65536
def csv_url(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/invites/csv"


async def test_the_csv_route_allows_the_file_limit_plus_65536(api, forbidden_routes):
    world = await api.seed_org()
    csv_limit = settings.CSV_MAX_BYTES + 65536
    big = b"email\n" + b"".join(f"s{i}@amity.edu\n".encode() for i in range(200))        # ~2.6 KB: over MAX_REQUEST_BYTES (1000)
    assert LIMIT < len(big) < csv_limit
    # over the generic limit but under the CSV limit: the CSV route does not answer 413 (it reaches the route code)
    r = await api.client.post(csv_url(world), files={"file": ("a.csv", big, "text/csv")}, headers=headers(api, world.owner))
    assert r.status_code == 500 and forbidden_routes == [1]                               # the patched upload ran and failed: not 413
    # over CSV_MAX_BYTES + 65536: 413 before the route
    forbidden_routes.clear()
    huge = b"email\n" + b"a" * (csv_limit + 10)
    r = await api.client.post(csv_url(world), files={"file": ("a.csv", huge, "text/csv")}, headers=headers(api, world.owner))
    assert_413(r, csv_limit)
    assert forbidden_routes == [] and await count(api, Invite) == 0 and await count(api, IdempotencyKey) == 0


async def test_the_csv_limit_applies_only_to_the_csv_route(api, forbidden_routes):
    world = await api.seed_org()
    body = b"x" * (LIMIT + 1)
    other = await api.client.post(f"/org/v1/orgs/{world.org.id}/invite-links", content=body,
                                  headers={**headers(api, world.owner), "Content-Type": "application/json"})
    assert_413(other)
    fake = await api.client.post(f"/org/v1/orgs/{world.org.id}/invites/csv/extra", content=body, headers=headers(api, world.owner))
    assert_413(fake)                                                                      # a look-alike path gets the generic limit


async def test_a_chunked_csv_over_its_limit_is_413(api, forbidden_routes):
    world = await api.seed_org()
    csv_limit = settings.CSV_MAX_BYTES + 65536

    async def chunks():
        for _ in range(0, csv_limit + 5000, 8000):
            yield b"q" * 8000

    r = await api.client.post(csv_url(world), content=chunks(),
                              headers={**headers(api, world.owner), "Content-Type": "multipart/form-data; boundary=zzz"})
    assert_413(r, csv_limit)
    assert forbidden_routes == []


async def test_the_default_limit_is_262144_and_the_csv_route_one_mebibyte_plus_64k():
    from app.core.config import Settings
    fresh = Settings(_env_file=None)
    assert fresh.MAX_REQUEST_BYTES == 262144
    assert fresh.CSV_MAX_BYTES + 65536 == 1_048_576 + 65536
