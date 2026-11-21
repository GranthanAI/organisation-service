"""Keep known_users in step with logged-in users (the Kafka history only reaches back to the topic's retention).

After a JWT passes, `sync_known_user` makes sure a row exists with the token's email. It uses its OWN short
transaction, never touches a deleted user, never sets email_verified_at, and never fails the request: a problem
is logged. "Already in sync" is remembered per (user_id, email) in process memory for 10 minutes, so a normal request
does no write.
"""

import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import system_clock
from app.core.logging import logger
from app.db.session import unit_of_work
from app.repositories.known_user_repository import KnownUserRepository

CACHE_TTL_SECONDS = 600.0
_MAX_ENTRIES = 50_000
_in_sync: dict[tuple[uuid.UUID, str], float] = {}


def clear_cache() -> None:
    _in_sync.clear()


def _fresh(user_id: uuid.UUID, email: str, now: float) -> bool:
    expires = _in_sync.get((user_id, email))
    return expires is not None and expires > now


def _remember(user_id: uuid.UUID, email: str, now: float) -> None:
    if len(_in_sync) >= _MAX_ENTRIES:
        for key in [k for k, v in _in_sync.items() if v <= now] or list(_in_sync)[: _MAX_ENTRIES // 2]:
            _in_sync.pop(key, None)
    _in_sync[(user_id, email)] = now + CACHE_TTL_SECONDS


async def sync_known_user(session: AsyncSession, user_id: uuid.UUID, email: str, known) -> None:
    """`known` is the row the caller already read (or None). Call only for a user that is not deleted."""
    now = time.monotonic()
    if _fresh(user_id, email, now):
        return
    if known is not None and known.email == email:
        _remember(user_id, email, now)
        return
    try:
        factory = async_sessionmaker(bind=session.bind, expire_on_commit=False, autoflush=False)
        async with unit_of_work(factory) as own:
            await KnownUserRepository(own).sync_email(user_id, email, system_clock.now())
        _remember(user_id, email, now)
    except Exception as exc:   # never fail a request over this
        logger.warning("could not sync known_users from a login", error=type(exc).__name__)
