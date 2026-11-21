"""Service-to-service identity for /internal/*: a per-caller bearer token from INTERNAL_SERVICE_TOKENS."""

import hmac
from functools import lru_cache

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import Unauthenticated
from app.core.logging import logger
from app.security.models import ServiceCaller


def parse_service_tokens(raw: str) -> list[tuple[str, str]]:
    """"name:token,name:token" -> [(name, token)]. Refuses empty names, empty tokens, duplicate names and a token
    shared by two names (it would make the caller's identity ambiguous). Values are never put in the message."""
    pairs: list[tuple[str, str]] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, token = item.partition(":")
        name, token = name.strip(), token.strip()
        if not sep or not name:
            raise ValueError("INTERNAL_SERVICE_TOKENS entries must look like name:token with a non-empty name")
        if not token:
            raise ValueError(f"INTERNAL_SERVICE_TOKENS entry {name!r} has an empty token")
        pairs.append((name, token))
    names = [n for n, _ in pairs]
    if len(set(names)) != len(names):
        raise ValueError("INTERNAL_SERVICE_TOKENS has a duplicate service name")
    tokens = [t for _, t in pairs]
    if len(set(tokens)) != len(tokens):
        raise ValueError("INTERNAL_SERVICE_TOKENS gives two services the same token")
    return pairs


@lru_cache(maxsize=1)
def load_service_tokens() -> tuple[tuple[str, str], ...]:
    """Parsed once (called at startup, so a bad value stops the service from booting)."""
    return tuple(parse_service_tokens(settings.INTERNAL_SERVICE_TOKENS))


def match_service_token(presented: str, pairs) -> str | None:
    """Compare against EVERY token with hmac.compare_digest and never return early, so timing does not reveal
    which entry matched (or how far a wrong token got)."""
    presented_bytes = presented.encode("utf-8")
    matched: str | None = None
    for name, token in pairs:
        if hmac.compare_digest(presented_bytes, token.encode("utf-8")):
            matched = name
    return matched


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


async def require_service(request: Request) -> ServiceCaller:
    """FastAPI dependency for /internal/*. A missing or wrong token gets 401 and writes nothing to the database;
    the only log line carries the path, never the header or the token."""
    presented = _bearer(request)
    matched = match_service_token(presented or "", load_service_tokens())
    if presented is None or matched is None:
        logger.warning("internal request refused", path=request.url.path)
        raise Unauthenticated()
    caller = ServiceCaller(name=matched)
    request.state.service_name = caller.name
    return caller
