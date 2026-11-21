"""Exponential backoff and an interruptible sleep, shared by the workers."""

import asyncio


def backoff_seconds(attempts: int, base: float = 1.0, cap: float = 600.0) -> float:
    """1 s after the first failure, then doubling (2, 4, 8, ...), capped at 10 minutes."""
    return min(cap, base * (2 ** max(0, attempts - 1)))


async def sleep_or_stop(stop, seconds: float) -> bool:
    """Wait `seconds`, or until `stop` (an asyncio.Event) is set. True if stop was set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False
