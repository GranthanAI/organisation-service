"""Global email send rate (N1): at most EMAIL_SENDS_PER_MINUTE emails a minute across every worker and process.

`acquire(clock)` is called by `invite.send` right before it sends. It takes one token from a Redis fixed-window counter
`org:email_send:<minute>`. It returns None (go ahead) or the time the caller should try again (the start of the next
minute; the job is deferred, which is not a failure and not an attempt).

Redis down: mail must not stop, so each process lets one email through per second (and logs a warning); the rest defer
by a second.
"""

from datetime import datetime, timedelta

from app.clients.redis_client import get_redis
from app.core.clock import Clock
from app.core.config import settings
from app.core.logging import logger

_last_fallback_send: datetime | None = None


def reset_fallback() -> None:
    global _last_fallback_send
    _last_fallback_send = None


async def acquire(clock: Clock) -> datetime | None:
    now = clock.now()
    minute = int(now.timestamp()) // 60
    try:
        client = get_redis()
        key = f"org:email_send:{minute}"
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 120)
            count, _ = await pipe.execute()
    except Exception:
        return _fallback(now)
    if count > settings.EMAIL_SENDS_PER_MINUTE:
        return datetime.fromtimestamp((minute + 1) * 60, tz=now.tzinfo)
    return None


def _fallback(now: datetime) -> datetime | None:
    global _last_fallback_send
    logger.warning("redis is down: email sending limited to one per second in this process")
    if _last_fallback_send is not None and now - _last_fallback_send < timedelta(seconds=1):
        return _last_fallback_send + timedelta(seconds=1)
    _last_fallback_send = now
    return None
