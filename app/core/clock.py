"""The only source of "now". Services take a Clock; tests pass a FixedClock. Do not call datetime.now() elsewhere."""

from datetime import datetime, timedelta, timezone


class Clock:
    """System clock: timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """A clock that only moves when told to (tests)."""

    def __init__(self, at: datetime) -> None:
        self._at = self._utc(at)

    @staticmethod
    def _utc(at: datetime) -> datetime:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        return at.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = self._utc(at)

    def advance(self, **delta) -> None:
        self._at += timedelta(**delta)


system_clock = Clock()
