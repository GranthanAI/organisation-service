"""organisation-service entrypoint: FastAPI app, lifespan, middleware, routers."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.clients.redis_client import close_redis
from app.core.config import settings
from app.core.logging import logger
from app.db.postgres import dispose_engine
from app.middleware.correlation import CorrelationIDMiddleware
from app.middleware.exception_handler import register_exception_handlers
from app.middleware.request_size import RequestSizeLimitMiddleware
from app.security.service_tokens import load_service_tokens
from app.telemetry import loop_lag
from app.workers.manager import WorkerManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting", environment=settings.ENVIRONMENT, port=settings.SERVICE_PORT,
                workers_enabled=settings.WORKERS_ENABLED)
    load_service_tokens()   # a malformed INTERNAL_SERVICE_TOKENS stops the service from booting
    workers = None
    if settings.WORKERS_ENABLED:   # outbox relay, consumer worker and job runner; a crash never takes the API down
        workers = WorkerManager()
        workers.start()
    lag_stop = asyncio.Event()
    lag_task = asyncio.create_task(loop_lag.run(lag_stop))
    yield
    logger.info("shutting down")
    lag_stop.set()
    await lag_task
    if workers is not None:
        await workers.stop()       # signal stop, wait up to 10 s, then cancel
    await dispose_engine()
    await close_redis()


app = FastAPI(
    title="GranthanAI organisation-service",
    description="Organisations, memberships, invites, seats and policies.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(RequestSizeLimitMiddleware)   # inside the correlation id, before any route reads a body
app.add_middleware(CorrelationIDMiddleware)
register_exception_handlers(app)
app.include_router(api_router)
