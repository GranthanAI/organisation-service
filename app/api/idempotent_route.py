"""Route class that makes POST routes idempotent: `router = APIRouter(route_class=IdempotentRoute)`.

A plain dependency cannot see the response body, so the wrapper runs around the whole handler. Use it on every
public and admin router; the Idempotency-Key header is then required on each POST (422 VALIDATION_ERROR otherwise).
Internal routes (service-to-service) do not use it.

Only 2xx responses are stored. Every 4xx and 5xx, and any exception, releases the reservation so the client can retry
(IDEMPOTENCY_MISMATCH and IDEMPOTENCY_IN_PROGRESS are answered before the handler and are never stored). The caller is
authenticated with the same helper as get_current_user before anything is reserved or replayed, so a revoked token or
a deleted user can never read a stored response.
"""

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import get_db_session, get_redis_client
from app.api.route_template import route_template
from app.core.constants import IDEMPOTENCY_HEADER, IDEMPOTENCY_REPLAY_HEADER
from app.core.exceptions import DomainError
from app.middleware.exception_handler import domain_error_response, http_error_response, validation_response
from app.security.dependencies import authenticate_user, bearer_token
from app.security.models import CurrentUser
from app.services.idempotency_service import (
    IdempotencyService,
    default_idempotency_service,
    request_hash,
    validate_key,
)


async def _authenticate(request: Request, token: str) -> CurrentUser:
    """The same checks as get_current_user (one shared helper). It uses the app's own providers, so tests'
    dependency overrides apply."""
    overrides = request.app.dependency_overrides
    redis = overrides.get(get_redis_client, get_redis_client)()
    session_provider = overrides.get(get_db_session, get_db_session)
    async with asynccontextmanager(session_provider)() as session:
        return await authenticate_user(request.method, token, redis, session)


def _service(request: Request) -> IdempotencyService:
    return getattr(request.app.state, "idempotency_service", None) or default_idempotency_service()


def _json_body(response: Response) -> Any:
    return json.loads(bytes(response.body))   # raises for streaming or non-JSON responses


async def _hashable_body(request: Request) -> bytes:
    """The body to fingerprint. A multipart upload carries a random boundary that changes on every send, so a client
    retrying the same file with the same key would look like a different request: the boundary is replaced by a constant."""
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if content_type.lower().startswith("multipart/form-data") and "boundary=" in content_type:
        boundary = content_type.split("boundary=", 1)[1].split(";")[0].strip().strip('"')
        if boundary:
            return body.replace(boundary.encode(), b"BOUNDARY")
    return body


class IdempotentRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if request.method != "POST":
                return await original(request)
            token = bearer_token(request)
            if token is None:
                return await original(request)   # no credentials: the route's own dependency answers 401
            route = route_template(request)   # the template ("/org/v1/orgs/{org_id}/invites"), not the concrete path
            actor = (await _authenticate(request, token)).id
            key = validate_key(request.headers.get(IDEMPOTENCY_HEADER))
            service = _service(request)
            stored = await service.begin(actor, route, key, request_hash(await _hashable_body(request)))
            if stored is not None:
                return JSONResponse(stored.body, status_code=stored.status_code,
                                    headers={IDEMPOTENCY_REPLAY_HEADER: "true"})
            try:
                try:
                    response = await original(request)
                except DomainError as exc:
                    response = domain_error_response(request, exc)
                except RequestValidationError as exc:
                    response = validation_response(request, exc)
                except StarletteHTTPException as exc:
                    response = http_error_response(request, exc)
                if not 200 <= response.status_code < 300:   # only successes are stored
                    await service.release(actor, route, key)
                    return response
                try:
                    body = _json_body(response)
                except Exception:
                    await service.release(actor, route, key)   # not a JSON body: cannot be replayed
                    return response
                await service.complete(actor, route, key, response.status_code, body)
                return response
            except BaseException:
                await service.release(actor, route, key)   # an unexpected exception: the client may retry
                raise

        return handler
