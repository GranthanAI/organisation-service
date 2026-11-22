"""Redis fixed-window rate limits as dependencies.

    Depends(rate_limit(RATE_INVITE_CREATE))                  # or rate_limit("invite_create", 30, 60, "user", True)

INCR and EXPIRE go in one pipeline. Over the limit: 429 RATE_LIMITED with Retry-After (seconds to the end of the
window). Redis down: `fail_closed` buckets answer 503 UPSTREAM_UNAVAILABLE; the others let the request through.
"""

import ipaddress
from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.api.deps import get_clock, get_redis_client
from app.core.clock import Clock
from app.core.config import settings
from app.core.constants import RateLimitBucket
from app.core.exceptions import RateLimited, UpstreamUnavailable
from app.core.logging import logger
from app.security.dependencies import get_current_user
from app.security.models import CurrentUser


def _parse_networks(raw: str) -> tuple[bool, list]:
    trust_all, nets = False, []
    for item in (raw or "").split(","):
        item = item.strip()
        if item == "*":
            trust_all = True
        elif item:
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                logger.warning("ignoring an invalid FORWARDED_ALLOW_IPS entry")
    return trust_all, nets


def _trusted(ip: str, trust_all: bool, nets: list) -> bool:
    if trust_all:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def client_ip(request: Request, allow_ips: str | None = None) -> str:
    """The caller's IP. X-Forwarded-For is believed only when the direct peer is a trusted proxy
    (FORWARDED_ALLOW_IPS); then the right-most address that is not itself a trusted proxy is the client."""
    peer = request.client.host if request.client else "unknown"
    trust_all, nets = _parse_networks(settings.FORWARDED_ALLOW_IPS if allow_ips is None else allow_ips)
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded or not _trusted(peer, trust_all, nets):
        return peer
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop, trust_all, nets):
            return hop
    return hops[0] if hops else peer


async def hit(redis: Redis, clock: Clock, bucket: str, who: str, limit: int, window_seconds: int) -> None:
    """Count one request; raise RateLimited when the window's count passes `limit`."""
    now = int(clock.now().timestamp())
    window = now // window_seconds
    redis_key = f"rl:{bucket}:{who}:{window}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds + 1)
        count, _ = await pipe.execute()
    if count > limit:
        raise RateLimited(window_seconds - (now % window_seconds))


def rate_limit(
    bucket: RateLimitBucket | str,
    limit: int | None = None,
    window_seconds: int | None = None,
    key: str | None = None,
    fail_closed: bool | None = None,
) -> Callable[..., Awaitable[None]]:
    """Dependency factory. Pass a RateLimitBucket from app/core/constants.py, or the five values."""
    if isinstance(bucket, RateLimitBucket):
        name, limit, window_seconds, key, fail_closed = (
            bucket.name, bucket.limit, bucket.window_seconds, bucket.key, bucket.fail_closed)
    else:
        name = bucket
    if limit is None or window_seconds is None or key not in ("user", "ip") or fail_closed is None:
        raise ValueError("rate_limit needs limit, window_seconds, key ('user' or 'ip') and fail_closed")

    async def check(request: Request, who: str, redis: Redis, clock: Clock) -> None:
        try:
            await hit(redis, clock, name, who, limit, window_seconds)
        except RateLimited:
            raise
        except Exception:
            if fail_closed:
                raise UpstreamUnavailable("Rate limiting is unavailable, try again shortly") from None
            logger.warning("rate limit store unavailable, letting the request through", bucket=name)

    if key == "user":
        async def by_user(
            request: Request,
            user: CurrentUser = Depends(get_current_user),
            redis: Redis = Depends(get_redis_client),
            clock: Clock = Depends(get_clock),
        ) -> None:
            await check(request, str(user.id), redis, clock)

        return by_user

    async def by_ip(
        request: Request,
        redis: Redis = Depends(get_redis_client),
        clock: Clock = Depends(get_clock),
    ) -> None:
        await check(request, client_ip(request), redis, clock)

    return by_ip


async def hit_fail_open(redis: Redis, clock: Clock, bucket: RateLimitBucket, who: str) -> None:
    """For limits keyed by something other than the caller (a domain): count one request, raise RateLimited over the
    limit, and let the request through when the store is down."""
    try:
        await hit(redis, clock, bucket.name, who, bucket.limit, bucket.window_seconds)
    except RateLimited:
        raise
    except Exception:
        logger.warning("rate limit store unavailable, letting the request through", bucket=bucket.name)
