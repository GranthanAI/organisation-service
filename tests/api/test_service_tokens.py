"""Service identity for /internal/*: parsing, constant-time matching and GET /internal/ping on the real app."""

import hmac

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.security import service_tokens
from app.security.service_tokens import load_service_tokens, match_service_token, parse_service_tokens


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --- parsing ---
def test_parse_a_list():
    assert parse_service_tokens("billing:aaa,gateway:bbb") == [("billing", "aaa"), ("gateway", "bbb")]
    assert parse_service_tokens(" billing : aaa , ") == [("billing", "aaa")]
    assert parse_service_tokens("") == []


@pytest.mark.parametrize("raw", [":token", "  :token", "noseparator", "billing:", "billing: "])
def test_empty_names_empty_tokens_and_malformed_entries_are_refused(raw):
    with pytest.raises(ValueError):
        parse_service_tokens(raw)


def test_duplicate_names_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        parse_service_tokens("billing:aaa,billing:bbb")


def test_a_token_shared_by_two_services_is_refused():
    with pytest.raises(ValueError, match="same token"):
        parse_service_tokens("billing:aaa,gateway:aaa")


def test_parse_errors_never_contain_the_token():
    for raw in ("billing:supersecret1,billing:supersecret2", "a:tok-xyz,b:tok-xyz", "name-only"):
        with pytest.raises(ValueError) as exc:
            parse_service_tokens(raw)
        assert "supersecret" not in str(exc.value) and "tok-xyz" not in str(exc.value)


def test_load_service_tokens_parses_the_setting_once(monkeypatch):
    load_service_tokens.cache_clear()
    monkeypatch.setattr(service_tokens.settings, "INTERNAL_SERVICE_TOKENS", "a:111,b:222")
    first = load_service_tokens()
    monkeypatch.setattr(service_tokens.settings, "INTERNAL_SERVICE_TOKENS", "changed:333")
    assert load_service_tokens() is first and first == (("a", "111"), ("b", "222"))
    load_service_tokens.cache_clear()


# --- matching ---
def test_match_returns_the_name_of_the_matching_entry():
    pairs = [("billing", "aaa"), ("gateway", "bbb")]
    assert match_service_token("bbb", pairs) == "gateway"
    assert match_service_token("aaa", pairs) == "billing"
    assert match_service_token("ccc", pairs) is None
    assert match_service_token("", pairs) is None


def test_match_compares_against_every_token_with_compare_digest(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(service_tokens.hmac, "compare_digest", spy)
    pairs = [("a", "tok-a"), ("b", "tok-b"), ("c", "tok-c")]
    assert match_service_token("tok-a", pairs) == "a"            # the FIRST entry matches ...
    assert len(calls) == 3                                       # ... and all three are still compared
    calls.clear()
    assert match_service_token("nope", pairs) is None
    assert len(calls) == 3


# --- GET /internal/ping on the real app ---
async def test_no_token_gives_401_problem_json(client):
    r = await client.get("/internal/ping")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json()["code"] == "UNAUTHENTICATED" and r.json()["status"] == 401 and r.json()["correlation_id"]


@pytest.mark.parametrize("header", ["Bearer wrong-token", "Bearer ", "Basic dev-billing-token", "dev-billing-token",
                                    "Bearer dev-billing-token-and-more"])
async def test_a_wrong_token_gives_401(client, header):
    r = await client.get("/internal/ping", headers={"Authorization": header})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"


async def test_the_right_token_returns_the_caller(client):
    r = await client.get("/internal/ping", headers={"Authorization": "Bearer dev-billing-token"})
    assert r.status_code == 200 and r.json() == {"caller": "billing"}
    r = await client.get("/internal/ping", headers={"Authorization": "bearer dev-gateway-token"})
    assert r.status_code == 200 and r.json() == {"caller": "gateway"}


async def test_a_user_jwt_is_not_a_service_token(client):
    from tests.support.tokens import make_token
    r = await client.get("/internal/ping", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 401


async def test_refused_requests_log_no_secret(client, caplog):
    await client.get("/internal/ping", headers={"Authorization": "Bearer my-secret-guess-123"})
    await client.get("/internal/ping")
    assert "my-secret-guess-123" not in caplog.text and "Bearer" not in caplog.text
    assert "dev-billing-token" not in caplog.text
    assert "internal request refused" in caplog.text


async def test_a_refused_request_writes_nothing_to_the_database(client, monkeypatch):
    from app.db import postgres
    monkeypatch.setattr(postgres, "get_engine", lambda: (_ for _ in ()).throw(AssertionError("database touched")))
    r = await client.get("/internal/ping", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
