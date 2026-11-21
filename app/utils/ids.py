"""UUIDv7 (RFC 9562): 48-bit millisecond timestamp, version 7, 12-bit counter, variant 10, 62 random bits.

Python 3.12 has no uuid7. Ids from one generator are strictly increasing: within the same millisecond (or if the clock
steps back) the 12-bit counter is incremented, and on overflow the timestamp borrows forward by 1 ms.
"""

import secrets
import threading
import time
import uuid
from collections.abc import Callable


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class Uuid7Generator:
    def __init__(self, now_ms: Callable[[], int] = _now_ms) -> None:
        self._now_ms = now_ms
        self._lock = threading.Lock()
        self._last_ms = 0
        self._last_seq = 0

    def new(self) -> uuid.UUID:
        with self._lock:
            ms = self._now_ms()
            if ms > self._last_ms:
                self._last_ms = ms
                self._last_seq = secrets.randbits(11)  # top bit clear: room to count up within this millisecond
            else:
                self._last_seq += 1
                if self._last_seq > 0xFFF:
                    self._last_ms += 1
                    self._last_seq = secrets.randbits(11)
            ms, seq = self._last_ms, self._last_seq
        value = (ms & 0xFFFFFFFFFFFF) << 80
        value |= 0x7 << 76
        value |= seq << 64
        value |= 0b10 << 62
        value |= secrets.randbits(62)
        return uuid.UUID(int=value)


_default = Uuid7Generator()


def uuid7() -> uuid.UUID:
    """A new UUIDv7 from the process-wide generator."""
    return _default.new()


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """The millisecond timestamp encoded in a UUIDv7."""
    return value.int >> 80
