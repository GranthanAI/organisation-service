"""Mounts the route groups: health (root), v1 (/org/v1), internal (/internal), admin (/admin/org/v1).

Phase 0 has health only; the other groups are added in the phases that build them.
"""

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router)
