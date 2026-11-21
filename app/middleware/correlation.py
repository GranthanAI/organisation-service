"""Correlation ID: accept X-Correlation-ID or generate one, keep it in a ContextVar, echo it back."""

import re
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")

CORRELATION_ID_HEADER = "X-Correlation-ID"
# Only short, plain values are accepted from callers (they end up in logs and response headers).
_VALID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def get_correlation_id() -> str:
    return _correlation_id_ctx.get("")


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = request.headers.get(CORRELATION_ID_HEADER, "")
        correlation_id = supplied if _VALID.match(supplied) else str(uuid.uuid4())
        request.state.correlation_id = correlation_id   # for handlers that run outside this middleware (500s)
        token = _correlation_id_ctx.set(correlation_id)
        try:
            response: Response = await call_next(request)
        finally:
            _correlation_id_ctx.reset(token)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
