"""Health, readiness and metrics. Mounted at the root (not under /org/v1)."""

import asyncio

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import Probe, get_probes
from app.core.config import settings
from app.core.logging import logger
from app.telemetry.metrics import render_all

router = APIRouter(tags=["Health"])


@router.get("/live")
async def live():
    return {"status": "UP"}


# Postgres is the only hard dependency: without it no request can be served correctly. Redis (rate limits, caches)
# and Kafka (outbox publishing lags but loses nothing) degrade the service; they are reported, not fatal.
REQUIRED = ["postgres"]


@router.get("/ready")
async def ready(response: Response, probes: dict[str, Probe] = Depends(get_probes)):
    async def run(name: str, probe: Probe) -> tuple[str, bool]:
        try:
            return name, bool(await asyncio.wait_for(probe(), timeout=settings.READY_PROBE_TIMEOUT_SECONDS))
        except Exception:  # includes the timeout
            return name, False

    results = dict(await asyncio.gather(*(run(n, p) for n, p in probes.items())))
    details = {name: ("UP" if ok else "DOWN") for name, ok in results.items()}
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        logger.warning("readiness check failed", failed=failed)
    if any(name in REQUIRED for name in failed):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        overall = "DOWN"
    else:
        overall = "DEGRADED" if failed else "UP"
    return {"status": overall, "required": REQUIRED, "failed": failed, "details": details}


@router.get("/metrics", include_in_schema=False)
async def metrics():
    body, content_type = render_all()
    return Response(content=body, media_type=content_type)
