"""Health, readiness and metrics. Mounted at the root (not under /org/v1)."""

import asyncio

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import Probe, get_probes
from app.core.logging import logger
from app.telemetry.metrics import render_all

router = APIRouter(tags=["Health"])


@router.get("/live")
async def live():
    return {"status": "UP"}


@router.get("/ready")
async def ready(response: Response, probes: dict[str, Probe] = Depends(get_probes)):
    async def run(name: str, probe: Probe) -> tuple[str, bool]:
        try:
            return name, bool(await probe())
        except Exception:
            return name, False

    results = dict(await asyncio.gather(*(run(n, p) for n, p in probes.items())))
    details = {name: ("UP" if ok else "DOWN") for name, ok in results.items()}
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        logger.warning("readiness failed", failed=failed)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "DOWN", "failed": failed, "details": details}
    return {"status": "UP", "details": details}


@router.get("/metrics", include_in_schema=False)
async def metrics():
    body, content_type = render_all()
    return Response(content=body, media_type=content_type)
