"""Mounts the route groups: health (root), v1 (/org/v1), internal (/internal), admin (/admin/org/v1).

Routes: health at the root, the organisation API (/org/v1), billing's internal routes, and the staff routes.
"""

from fastapi import APIRouter, Depends

from app.api.admin import orgs as admin_orgs
from app.api.internal import memberships, orgs, ping
from app.api.internal.timing import time_request
from app.api.v1 import audit, domains, health, invites, me, members, policies
from app.api.v1 import orgs as v1_orgs
from app.security.service_tokens import require_service

api_router = APIRouter()
api_router.include_router(health.router)

# Public routes, reached through api-gateway as /org/v1/*. Every router here uses IdempotentRoute (POSTs need an
# Idempotency-Key).
v1_router = APIRouter(prefix="/org/v1")
v1_router.include_router(me.router)
v1_router.include_router(v1_orgs.router)
v1_router.include_router(members.router)
v1_router.include_router(audit.router)
v1_router.include_router(invites.public_router)    # GET /org/v1/invites/preview: the one route without a JWT
v1_router.include_router(invites.router)
v1_router.include_router(domains.router)
v1_router.include_router(policies.router)
api_router.include_router(v1_router)

# Service-to-service routes. Never routed by the gateway; every one needs a service token.
internal_router = APIRouter(prefix="/internal", tags=["Internal"],
                            dependencies=[Depends(require_service), Depends(time_request)])
internal_router.include_router(ping.router)
internal_router.include_router(memberships.router)
internal_router.include_router(orgs.router)
api_router.include_router(internal_router)

# Staff routes, reached through api-gateway as /admin/org/v1/*: GranthanAI staff only.
admin_router = APIRouter(prefix="/admin/org/v1")
admin_router.include_router(admin_orgs.router)
api_router.include_router(admin_router)
