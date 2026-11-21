"""Idempotency for public and admin POSTs.

Scope is (actor_id, route template, Idempotency-Key). Flow:
  1. begin(): reserve in_progress with a 60 s lock, in its own short transaction.
  2. On a key conflict read the row: same hash and done -> replay; different hash -> IDEMPOTENCY_MISMATCH;
     in_progress and locked -> IDEMPOTENCY_IN_PROGRESS (409, Retry-After); lock passed (or older than 24 h) -> take over.
  3. After the handler: complete() on 2xx/4xx, release() (delete the reservation) on 5xx or an exception.
Each step uses its own unit of work, so the reservation is visible to a concurrent request at once.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, system_clock
from app.core.constants import IDEMPOTENCY_LOCK_SECONDS, IDEMPOTENCY_TTL_HOURS
from app.core.exceptions import IdempotencyInProgress, IdempotencyMismatch, ValidationFailed
from app.db.session import unit_of_work
from app.repositories.idempotency_repository import IdempotencyRepository

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def validate_key(value: str | None) -> str:
    """The Idempotency-Key header: 8-128 characters of [A-Za-z0-9_-]. Required."""
    if not value or not KEY_PATTERN.match(value):
        raise ValidationFailed("Idempotency-Key header is required: 8-128 characters of letters, digits, _ or -")
    return value


def request_hash(body: bytes) -> str:
    """sha256 of the canonical JSON body (sorted keys, no whitespace); non-JSON bodies hash as raw bytes."""
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    except (ValueError, UnicodeDecodeError):
        canonical = body
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: Any


class IdempotencyService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None,
                 clock: Clock = system_clock) -> None:
        self._factory = session_factory
        self._clock = clock

    async def begin(self, actor_id: uuid.UUID, route: str, key: str, body_hash: str) -> StoredResponse | None:
        """None: this caller owns the reservation and must run the handler. A StoredResponse: replay it."""
        now = self._clock.now()
        locked_until = now + timedelta(seconds=IDEMPOTENCY_LOCK_SECONDS)
        async with unit_of_work(self._factory) as s:
            repo = IdempotencyRepository(s)
            if await repo.reserve(actor_id, route, key, body_hash, now, locked_until):
                return None
            row = await repo.get(actor_id, route, key)
            if row is None:   # deleted between the insert attempt and the read: start over
                if await repo.reserve(actor_id, route, key, body_hash, now, locked_until):
                    return None
                raise IdempotencyInProgress(1)
            expired_before = now - timedelta(hours=IDEMPOTENCY_TTL_HOURS)
            if row.created_at > expired_before:
                if row.request_hash != body_hash:
                    raise IdempotencyMismatch("This Idempotency-Key was used with a different request body")
                if row.state == "done":
                    return StoredResponse(row.status_code, row.response)
                if row.locked_until is not None and row.locked_until > now:
                    raise IdempotencyInProgress(int((row.locked_until - now).total_seconds()) + 1)
            if await repo.take_over(actor_id, route, key, body_hash, now, locked_until, expired_before):
                return None
            raise IdempotencyInProgress(1)

    async def complete(self, actor_id: uuid.UUID, route: str, key: str, status_code: int, body: Any) -> None:
        async with unit_of_work(self._factory) as s:
            await IdempotencyRepository(s).complete(actor_id, route, key, status_code, body)

    async def release(self, actor_id: uuid.UUID, route: str, key: str) -> None:
        """Drop the reservation so the client can retry (5xx, or an exception)."""
        async with unit_of_work(self._factory) as s:
            await IdempotencyRepository(s).delete(actor_id, route, key)


_default: IdempotencyService | None = None


def default_idempotency_service() -> IdempotencyService:
    global _default
    if _default is None:
        _default = IdempotencyService()
    return _default
