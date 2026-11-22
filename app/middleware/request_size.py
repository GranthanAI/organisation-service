"""Request size limit (O2), the outermost thing a request meets after the correlation id: no route, dependency or
IdempotentRoute reads a body that is over the limit.

`MAX_REQUEST_BYTES` for every route; the CSV upload route gets `CSV_MAX_BYTES + 65536` (the file plus multipart framing).
A Content-Length over the limit is answered 413 at once, before the app runs. Without one (chunked), the bytes are
counted as they stream and the app is cut off with 413 once the count passes the limit. The 413 is problem+json with
code REQUEST_TOO_LARGE.
"""

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.middleware.exception_handler import problem_response

CSV_UPLOAD_PATH = re.compile(r"^/org/v1/orgs/[^/]+/invites/csv/?$")
MULTIPART_ALLOWANCE = 65536


def limit_for(method: str, path: str) -> int:
    if method == "POST" and CSV_UPLOAD_PATH.match(path):
        return settings.CSV_MAX_BYTES + MULTIPART_ALLOWANCE
    return settings.MAX_REQUEST_BYTES


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope.get("method", "GET"), scope.get("path", ""))
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await self._refuse(scope, receive, send, limit)
            return

        seen = 0
        over = False
        responded = False

        async def counting_receive() -> Message:
            nonlocal seen, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    over = True
                    return {"type": "http.disconnect"}      # the app sees the client go away and stops reading
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal responded
            if over:
                return                                       # whatever the app says now (400, 500) is replaced by the 413
            responded = responded or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not over:
                raise
        if over and not responded:
            await self._refuse(scope, receive, send, limit)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = problem_response(None, 413, "REQUEST_TOO_LARGE", f"The request body is larger than {limit} bytes.")
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
