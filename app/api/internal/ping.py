"""GET /internal/ping: lets billing and the owner check that a service token is accepted."""

from fastapi import APIRouter, Depends

from app.security.models import ServiceCaller
from app.security.service_tokens import require_service

router = APIRouter()


@router.get("/ping")
async def ping(caller: ServiceCaller = Depends(require_service)) -> dict[str, str]:
    return {"caller": caller.name}
