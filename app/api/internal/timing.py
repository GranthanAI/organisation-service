"""org_internal_requests_seconds{route}: how long each /internal route takes (billing calls these on a cache miss)."""

import time

from fastapi import Request

from app.api.route_template import route_template
from app.telemetry.metrics import INTERNAL_REQUESTS


async def time_request(request: Request):
    started = time.perf_counter()
    try:
        yield
    finally:
        INTERNAL_REQUESTS.labels(route=route_template(request)).observe(time.perf_counter() - started)
