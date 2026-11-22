"""billing-service, as this service calls it (bearer BILLING_SERVICE_TOKEN, service to service; never the gateway).

  GET /internal/orgs/{org_id}/usage?from=&to=      the usage report (P10b)  {org_id, from, to, rows: [...]}
  GET /internal/orgs/{org_id}/subscription         the organisation's subscription (P10c; billing has not built it yet)

One retry on a connect error or a 5xx. Billing down, a 5xx, a timeout or a 401 (our own misconfiguration, logged as an
error) all surface as UpstreamUnavailable (503). Every call is timed in org_billing_requests_seconds{route,result}.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import UpstreamUnavailable
from app.core.logging import logger
from app.telemetry.metrics import BILLING_REQUESTS

USAGE_TIMEOUT_SECONDS = 10.0          # the usage report is not the hot path
SUBSCRIPTION_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Subscription:
    plan_code: str
    variant: str | None
    seats: int | None
    status: str
    period_end: datetime | None
    subscription_id: str | None = None
    pending_seats: int | None = None


class RouteMissing(Exception):
    """billing answered 404 for the ROUTE itself (it has not built it yet), not for an unknown organisation."""


class NoSubscription(Exception):
    """billing answered 404 for this organisation: it has no subscription."""


class BillingClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = (base_url or settings.BILLING_BASE_URL).rstrip("/")
        self._token = token or settings.BILLING_SERVICE_TOKEN
        self._transport = transport

    async def _get(self, route: str, path: str, params: dict[str, Any] | None, timeout: float) -> httpx.Response:
        """GET with one retry on a connect error or a 5xx; the answer, or UpstreamUnavailable."""
        headers = {"Authorization": f"Bearer {self._token}"}
        last: str = "no attempt"
        for _attempt in (1, 2):
            started = time.perf_counter()
            result = "error"
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                    response = await client.get(self._base + path, params=params, headers=headers)
                result = str(response.status_code // 100) + "xx"
                if response.status_code == 401:
                    logger.error("billing refused our service token (BILLING_SERVICE_TOKEN is wrong or not set up in billing)", route=route)
                    raise UpstreamUnavailable("billing-service is not available")
                if response.status_code >= 500:
                    last = f"status {response.status_code}"
                    continue                                              # retry once
                return response
            except httpx.TimeoutException:
                result, last = "timeout", "timeout"                       # a timeout is not retried: it already took long enough
                break
            except httpx.TransportError as exc:
                last = type(exc).__name__                                 # connection refused, reset...: retry once
            finally:
                BILLING_REQUESTS.labels(route=route, result=result).observe(time.perf_counter() - started)
        logger.warning("billing-service is unavailable", route=route, reason=last)
        raise UpstreamUnavailable("billing-service is not available")

    async def usage(self, org_id: uuid.UUID, start: date, end: date) -> dict:
        response = await self._get("usage", f"/internal/orgs/{org_id}/usage",
                                   {"from": start.isoformat(), "to": end.isoformat()}, USAGE_TIMEOUT_SECONDS)
        if response.status_code != 200:
            logger.warning("billing answered the usage report with an error", status=response.status_code)
            raise UpstreamUnavailable("billing-service could not produce the report")
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            logger.error("billing sent a usage report in an unexpected shape")
            raise UpstreamUnavailable("billing-service sent an unexpected answer")
        return body

    async def subscription(self, org_id: uuid.UUID) -> Subscription:
        """The org's subscription. NoSubscription: billing knows no subscription for it (404 naming the org). RouteMissing:
        the route does not exist in billing yet (a 404 that is not about an organisation)."""
        response = await self._get("subscription", f"/internal/orgs/{org_id}/subscription", None, SUBSCRIPTION_TIMEOUT_SECONDS)
        if response.status_code == 404:
            if _problem_code(response) == "NO_SUBSCRIPTION":      # billing's own answer: this organisation has no live subscription
                raise NoSubscription()
            raise RouteMissing()                                   # any other 404: billing has not built the route (yet)
        if response.status_code != 200:
            logger.warning("billing answered the subscription with an error", status=response.status_code)
            raise UpstreamUnavailable("billing-service could not answer")
        body = response.json()
        period_end = body.get("period_end")
        return Subscription(
            plan_code=body["plan_code"], variant=body.get("variant"), seats=body.get("seats"), status=body["status"],
            period_end=datetime.fromisoformat(period_end.replace("Z", "+00:00")) if period_end else None,
            subscription_id=body.get("subscription_id") or body.get("id"), pending_seats=body.get("pending_seats"))


def _problem_code(response: httpx.Response) -> str:
    """The `code` of a problem+json answer, or "" when the body is anything else (plain framework 404s, HTML, empty)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("code", "")).upper() if isinstance(body, dict) else ""


def get_billing_client() -> BillingClient:
    return BillingClient()
