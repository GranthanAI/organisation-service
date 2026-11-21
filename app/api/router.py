"""Mounts the route groups: health (root), v1 (/org/v1), internal (/internal), admin (/admin/org/v1).

Business routes arrive in later phases; admin is added in Phase 5.
"""

from fastapi import APIRouter, Depends

from app.api.internal import memberships, orgs, ping
from app.api.internal.timing import time_request
from app.api.v1 import health
from app.security.service_tokens import require_service

api_router = APIRouter()
api_router.include_router(health.router)

# Public routes, reached through api-gateway as /org/v1/*. Empty until Phase 6.
v1_router = APIRouter(prefix="/org/v1")
api_router.include_router(v1_router)

# Service-to-service routes. Never routed by the gateway; every one needs a service token.
internal_router = APIRouter(prefix="/internal", tags=["Internal"],
                            dependencies=[Depends(require_service), Depends(time_request)])
internal_router.include_router(ping.router)
internal_router.include_router(memberships.router)
internal_router.include_router(orgs.router)
api_router.include_router(internal_router)
