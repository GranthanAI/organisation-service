"""Route class that makes POST routes idempotent: `router = APIRouter(route_class=IdempotentRoute)`.

A plain dependency cannot see the response body, so the wrapper runs around the whole handler. Use it on every
public and admin router; the Idempotency-Key header is then required on each POST (422 VALIDATION_ERROR otherwise).
Internal routes (service-to-service) do not use it.
"""

import json
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.constants import IDEMPOTENCY_HEADER, IDEMPOTENCY_REPLAY_HEADER
from app.core.exceptions import DomainError
from app.middleware.exception_handler import (
    PROBLEM_JSON,
    domain_error_response,
    http_error_response,
    validation_response,
)
from app.security.jwt import verify_jwt_token
from app.services.idempotency_service import (
    IdempotencyService,
    default_idempotency_service,
    request_hash,
    validate_key,
)

# Not stored, so the client can simply retry: the answer depends on the credentials or on timing, not on the request.
NOT_STORED_STATUSES = frozenset({401, 429})


def _actor_id(request: Request) -> uuid.UUID | None:
    """The caller named by a verified bearer token, or None (the handler then answers 401 as usual)."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        return uuid.UUID(str(verify_jwt_token(token.strip())["sub"]))
    except Exception:
        return None


def _service(request: Request) -> IdempotencyService:
    return getattr(request.app.state, "idempotency_service", None) or default_idempotency_service()


def _json_body(response: Response) -> Any:
    return json.loads(bytes(response.body))   # raises for streaming or non-JSON responses


class IdempotentRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()
        route = self.path_format   # the template ("/org/v1/orgs/{org_id}/invites"), not the concrete path

        async def handler(request: Request) -> Response:
            if request.method != "POST":
                return await original(request)
            actor = _actor_id(request)
            if actor is None:
                return await original(request)
            key = validate_key(request.headers.get(IDEMPOTENCY_HEADER))
            service = _service(request)
            stored = await service.begin(actor, route, key, request_hash(await request.body()))
            if stored is not None:
                return JSONResponse(
                    stored.body, status_code=stored.status_code,
                    media_type=PROBLEM_JSON if stored.status_code >= 400 else "application/json",
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
                status = response.status_code
                if status >= 500 or status in NOT_STORED_STATUSES:
                    await service.release(actor, route, key)
                    return response
                try:
                    body = _json_body(response)
                except Exception:
                    await service.release(actor, route, key)   # not a JSON body: cannot be replayed
                    return response
                await service.complete(actor, route, key, status, body)
                return response
            except BaseException:
                await service.release(actor, route, key)   # an unexpected exception: the client may retry
                raise

        return handler
