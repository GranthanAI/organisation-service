"""application/problem+json for every error: one case per code, the validation and 500 shapes, route errors."""

import inspect

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core import exceptions as ex
from app.core.error_codes import ERROR_STATUS, INTERNAL_ERROR
from app.main import app as real_app
from app.middleware.correlation import CorrelationIDMiddleware
from app.middleware.exception_handler import register_exception_handlers


def all_error_classes() -> list[type[ex.DomainError]]:
    found, stack = [], [ex.DomainError]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            found.append(sub)
            stack.append(sub)
    return found


def make(cls: type[ex.DomainError]) -> ex.DomainError:
    params = [p for p in inspect.signature(cls.__init__).parameters if p != "self"]
    if not params:
        return cls()
    return cls(7) if params[0] == "retry_after" else cls("something specific")


# What docs/PHASES.md P3c says, written out independently of app/core/error_codes.py.
EXPECTED = {
    "SEATS_FULL": 409, "PILOT_FULL": 409, "ALREADY_IN_ORG": 409, "LAST_OWNER": 409, "ORG_INACTIVE": 409,
    "INVITE_ALREADY_OPEN": 409, "DOMAIN_TAKEN": 409, "INVITE_INVALID": 404, "INVITE_EXPIRED": 410,
    "INVITE_EMAIL_MISMATCH": 403, "EMAIL_NOT_VERIFIED": 403, "FEATURE_NOT_IN_PLAN": 403, "FORBIDDEN_ROLE": 403,
    "VERSION_CONFLICT": 412, "PUBLIC_EMAIL_DOMAIN": 422, "CSV_INVALID": 422, "VALIDATION_ERROR": 422,
    "RATE_LIMITED": 429, "UPSTREAM_UNAVAILABLE": 503,
    # added in Phase 3
    "NOT_FOUND": 404, "JOIN_REQUEST_PENDING": 409, "IDEMPOTENCY_MISMATCH": 422, "IDEMPOTENCY_IN_PROGRESS": 409,
    "UNAUTHENTICATED": 401,
}


class Body(BaseModel):
    age: int
    name: str


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIDMiddleware)
    register_exception_handlers(app)

    @app.get("/raise/{index}")
    async def raise_it(index: int):
        raise make(all_error_classes()[index])

    @app.post("/validate")
    async def validate(body: Body, limit: int = 5):
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("connection to db failed password=hunter2 host=10.0.0.5")

    return app


@pytest.fixture
async def client():
    transport = ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_every_domain_error_subclass_has_a_code_and_an_entry_in_the_one_map():
    classes = all_error_classes()
    assert len(classes) >= 25
    for cls in classes:
        assert cls.code in ERROR_STATUS, f"{cls.__name__} ({cls.code}) is not in app/core/error_codes.py"
        assert cls.code != "DOMAIN_ERROR", f"{cls.__name__} forgot to set a code"


def test_the_map_matches_phases_md_for_every_listed_code():
    for code, status in EXPECTED.items():
        assert ERROR_STATUS[code] == status, code


def test_every_code_in_the_map_is_raised_by_some_error_class():
    raised = {cls.code for cls in all_error_classes()} | {"DOMAIN_ERROR", INTERNAL_ERROR}
    assert set(ERROR_STATUS) == raised


@pytest.mark.parametrize("index", range(len(all_error_classes())), ids=lambda i: all_error_classes()[i].__name__)
async def test_each_error_class_gives_its_problem_json(client, index):
    cls = all_error_classes()[index]
    r = await client.get(f"/raise/{index}", headers={"X-Correlation-ID": "cid-abc"})
    body = r.json()
    assert r.status_code == ERROR_STATUS[cls.code]
    assert r.headers["content-type"].startswith("application/problem+json")
    assert set(body) == {"type", "title", "status", "code", "detail", "correlation_id"}
    assert body["code"] == cls.code and body["status"] == r.status_code and body["correlation_id"] == "cid-abc"
    assert r.headers["x-correlation-id"] == "cid-abc"
    assert body["type"].startswith("https://granthan.com/problems/") and body["title"] and body["detail"]


@pytest.mark.parametrize("cls, header", [(ex.RateLimited, "retry-after"), (ex.IdempotencyInProgress, "retry-after"),
                                         (ex.Unauthenticated, "www-authenticate")])
async def test_headers_travel_with_the_error(client, cls, header):
    index = all_error_classes().index(cls)
    r = await client.get(f"/raise/{index}")
    assert header in r.headers
    if header == "retry-after":
        assert int(r.headers[header]) >= 1


async def test_every_status_in_the_phases_table_is_actually_returned(client):
    seen = {}
    for index, cls in enumerate(all_error_classes()):
        seen[cls.code] = (await client.get(f"/raise/{index}")).status_code
    for code, status in EXPECTED.items():
        assert seen[code] == status, code


# --- validation ---
async def test_validation_error_lists_fields_and_never_echoes_input(client):
    r = await client.post("/validate?limit=notanint", json={"age": "SECRET-VALUE-123", "extra": "x"})
    body = r.json()
    assert r.status_code == 422 and body["code"] == "VALIDATION_ERROR"
    assert r.headers["content-type"].startswith("application/problem+json")
    fields = {e["field"] for e in body["errors"]}
    assert {"age", "name", "limit"} <= fields
    assert all(set(e) == {"field", "message"} for e in body["errors"])
    assert "SECRET-VALUE-123" not in r.text and "notanint" not in r.text


async def test_a_malformed_json_body_is_a_validation_error_too(client):
    r = await client.post("/validate", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


# --- 500 ---
async def test_an_unexpected_exception_is_a_500_with_no_internals(client, caplog):
    r = await client.get("/boom", headers={"X-Correlation-ID": "cid-500"})
    body = r.json()
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "INTERNAL_ERROR" and body["correlation_id"] == "cid-500"
    assert set(body) == {"type", "title", "status", "code", "detail", "correlation_id"}
    for leak in ("hunter2", "10.0.0.5", "RuntimeError", "Traceback", "boom"):
        assert leak not in r.text
    assert r.headers["x-correlation-id"] == "cid-500"
    # the log has what the body must not: the stack trace and the correlation id
    assert "RuntimeError" in caplog.text and "cid-500" in caplog.text


# --- unknown route and wrong method, on the real app ---
async def test_unknown_path_is_a_404_problem_json_on_the_real_app():
    async with AsyncClient(transport=ASGITransport(app=real_app), base_url="http://test") as c:
        r = await c.get("/no/such/path", headers={"X-Correlation-ID": "cid-404"})
        wrong_method = await c.post("/live")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "NOT_FOUND" and r.json()["correlation_id"] == "cid-404"
    assert wrong_method.status_code == 405 and wrong_method.json()["code"] == "METHOD_NOT_ALLOWED"
    assert "allow" in wrong_method.headers


async def test_health_still_works_with_the_handlers_installed():
    async with AsyncClient(transport=ASGITransport(app=real_app), base_url="http://test") as c:
        assert (await c.get("/live")).status_code == 200
