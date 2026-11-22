"""Correlation ID: accept X-Correlation-ID or generate one, keep it in a ContextVar, echo it back.

A plain ASGI middleware (not BaseHTTPMiddleware, which costs a task and a stream per request: measured in Q1)."""

import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")

CORRELATION_ID_HEADER = "X-Correlation-ID"
# Only short, plain values are accepted from callers (they end up in logs and response headers).
_VALID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HEADER_BYTES = CORRELATION_ID_HEADER.lower().encode()


def get_correlation_id() -> str:
    return _correlation_id_ctx.get("")


class CorrelationIDMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = ""
        for name, value in scope.get("headers", []):
            if name == _HEADER_BYTES:
                supplied = value.decode("latin-1")
                break
        correlation_id = supplied if _VALID.match(supplied) else str(uuid.uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id    # for handlers that run outside this middleware (500s)
        token = _correlation_id_ctx.set(correlation_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _correlation_id_ctx.reset(token)
