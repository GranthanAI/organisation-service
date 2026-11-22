"""Every error leaves as application/problem+json: {type, title, status, code, detail, correlation_id}.

DomainError -> the status from app/core/error_codes.py. RequestValidationError -> 422 VALIDATION_ERROR listing
{field, message} pairs (never the input values). Starlette HTTP errors (unknown route, wrong method) -> problem+json.
Anything else -> 500 INTERNAL_ERROR with no internals in the body; the stack trace goes to the log.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.route_template import route_template
from app.core.error_codes import ERROR_STATUS, HTTP_STATUS_CODES, INTERNAL_ERROR
from app.core.exceptions import DomainError
from app.core.logging import logger
from app.middleware.correlation import CORRELATION_ID_HEADER, get_correlation_id

PROBLEM_JSON = "application/problem+json"
PROBLEM_TYPE_BASE = "https://granthan.com/problems/"
_INTERNAL_DETAIL = "An unexpected error occurred."


def _correlation_id(request: Request | None = None) -> str:
    cid = get_correlation_id()
    if not cid and request is not None:
        cid = getattr(request.state, "correlation_id", "") or ""
    return cid


def problem_response(
    request: Request | None, status: int, code: str, detail: str, headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    cid = _correlation_id(request)
    # Every 4xx and 5xx is logged exactly once, here, where every problem+json response is made: the code, the route TEMPLATE
    # (never the concrete path with ids), the status and the correlation id. Never a body, a token or an email address.
    (logger.error if status >= 500 else logger.info)(
        "request failed", code=code, route=route_template(request) if request is not None else "-", status=status,
        method=request.method if request is not None else "-", correlation_id=cid)
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + code.lower().replace("_", "-"),
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "code": code,
        "detail": detail,
        "correlation_id": cid,
    }
    if extra:
        body.update(extra)
    out_headers = dict(headers or {})
    if cid:
        out_headers[CORRELATION_ID_HEADER] = cid   # the 500 handler runs outside the correlation middleware
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=out_headers)


def domain_error_response(request: Request | None, exc: DomainError) -> JSONResponse:
    status = ERROR_STATUS.get(exc.code, 500)
    return problem_response(request, status, exc.code, exc.detail or exc.code.replace("_", " ").capitalize(),
                            headers=exc.headers, extra=exc.extra)


def validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """{field, message} only: no `input`, no `ctx`, so a rejected value is never echoed back."""
    out = []
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc", ()) if part not in ("body", "query", "path", "header")]
        out.append({"field": ".".join(loc) or "request", "message": str(err.get("msg", "Invalid value"))})
    return out


def validation_response(request: Request | None, exc: RequestValidationError) -> JSONResponse:
    return problem_response(request, 422, "VALIDATION_ERROR", "The request is not valid.",
                            extra={"errors": validation_errors(exc)})


def http_error_response(request: Request | None, exc: StarletteHTTPException) -> JSONResponse:
    code = HTTP_STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return problem_response(request, exc.status_code, code, detail, headers=dict(exc.headers or {}))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        return domain_error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return validation_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return http_error_response(request, exc)

    @app.exception_handler(SQLAlchemyTimeoutError)
    async def _pool_exhausted(request: Request, exc: SQLAlchemyTimeoutError):
        logger.error("database connection pool exhausted", path=request.url.path, correlation_id=_correlation_id(request))
        return problem_response(request, 503, "UPSTREAM_UNAVAILABLE", "The service is busy, try again shortly.", headers={"Retry-After": "1"})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.error("unhandled exception", exc_info=exc, path=request.url.path, method=request.method,
                     correlation_id=_correlation_id(request))
        return problem_response(request, 500, INTERNAL_ERROR, _INTERNAL_DETAIL)


__all__ = ["register_exception_handlers", "problem_response", "ERROR_STATUS"]
