"""organisation-service entrypoint: FastAPI app, lifespan, middleware, routers."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.clients.redis_client import close_redis
from app.core.config import settings
from app.core.logging import logger
from app.db.postgres import dispose_engine
from app.middleware.correlation import CorrelationIDMiddleware
from app.middleware.exception_handler import register_exception_handlers
from app.security.service_tokens import load_service_tokens


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting", environment=settings.ENVIRONMENT, port=settings.SERVICE_PORT,
                workers_enabled=settings.WORKERS_ENABLED)
    load_service_tokens()   # a malformed INTERNAL_SERVICE_TOKENS stops the service from booting
    # Phase 4 starts the outbox worker, consumer worker and job runner here when WORKERS_ENABLED.
    yield
    logger.info("shutting down")
    await dispose_engine()
    await close_redis()


app = FastAPI(
    title="GranthanAI organisation-service",
    description="Organisations, memberships, invites, seats and policies.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIDMiddleware)
register_exception_handlers(app)
app.include_router(api_router)
