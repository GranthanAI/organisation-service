"""BillingClient.subscription(): the two kinds of 404, the answer's fields, and the failure modes (billing is faked with an httpx
mock transport)."""

import uuid
from datetime import datetime, timezone

import httpx
import pytest

from app.clients.billing_client import BillingClient, NoSubscription, RouteMissing
from app.core.exceptions import UpstreamUnavailable

ORG = uuid.uuid4()


def client(handler) -> BillingClient:
    return BillingClient("http://billing.test", "tok", httpx.MockTransport(handler))


async def test_a_404_with_the_code_no_subscription_means_the_org_has_none():
    body = {"type": "x", "title": "No subscription", "status": 404, "code": "NO_SUBSCRIPTION", "detail": f"organisation {ORG} has no subscription"}
    with pytest.raises(NoSubscription):
        await client(lambda r: httpx.Response(404, json=body)).subscription(ORG)


@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"detail": "Not Found"}),                                           # FastAPI's plain 404 for an unknown path
    httpx.Response(404, json={"code": "NOT_FOUND", "detail": "no such organisation"}),            # another code: NOT a "no subscription"
    httpx.Response(404, json={"code": "NOT_FOUND", "detail": "No organisation subscription route"}),   # words in the detail do not matter
    httpx.Response(404, text="<html>nope</html>"),
    httpx.Response(404, json=["not", "a", "dict"]),
    httpx.Response(404),
])
async def test_any_other_404_means_the_route_is_missing(response):
    with pytest.raises(RouteMissing):
        await client(lambda r: response).subscription(ORG)


async def test_the_answer_is_read_with_pending_seats_and_the_request_is_right():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"subscription_id": "sub-9", "plan_code": "org_pro", "variant": "college", "seats": 120,
                                         "pending_seats": 100, "status": "active", "period_end": "2027-04-01T00:00:00Z"})

    sub = await client(handler).subscription(ORG)
    assert (sub.plan_code, sub.variant, sub.seats, sub.pending_seats, sub.status, sub.subscription_id) == ("org_pro", "college", 120, 100, "active", "sub-9")
    assert sub.period_end == datetime(2027, 4, 1, tzinfo=timezone.utc)
    (request,) = seen
    assert request.url.path == f"/internal/orgs/{ORG}/subscription" and request.headers["authorization"] == "Bearer tok"


async def test_no_pending_seats_is_none():
    ok = httpx.Response(200, json={"subscription_id": "s", "plan_code": "payg", "variant": "company", "seats": None, "pending_seats": None,
                                   "status": "active", "period_end": None})
    sub = await client(lambda r: ok).subscription(ORG)
    assert sub.pending_seats is None and sub.seats is None and sub.period_end is None


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(401, json={"detail": "x"}), httpx.Response(422, json={})])
async def test_other_failures_are_unavailable(response):
    with pytest.raises(UpstreamUnavailable):
        await client(lambda r: response).subscription(ORG)
