"""Fakes for the background workers: no broker, no network."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable


class FakeProducer:
    """An EventProducer that records what it was asked to send. `fail_on` lists call numbers (1-based) that raise;
    `before_ack` runs inside send(), before the 'broker ack' returns (tests read the database there)."""

    def __init__(self, fail_on: set[int] | None = None, before_ack: Callable | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.calls = 0
        self.fail_on = fail_on or set()
        self.fail_always = False
        self.before_ack = before_ack

    async def send(self, topic: str, key: str, value: dict[str, Any], correlation_id: str | None = None) -> None:
        self.calls += 1
        if self.fail_always or self.calls in self.fail_on:
            raise ConnectionError("broker unavailable")
        if self.before_ack is not None:
            await self.before_ack(topic, key, value)
        self.sent.append({"topic": topic, "key": key, "value": value, "correlation_id": correlation_id})

    def topics(self) -> list[str]:
        return [m["topic"] for m in self.sent]


@dataclass
class FakeMessage:
    topic: str
    value: bytes
    key: bytes | None = None
    partition: int = 0
    offset: int = 0
    headers: list = field(default_factory=list)


def message(topic: str, body: dict | bytes, key: str | None = None, offset: int = 0, partition: int = 0) -> FakeMessage:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return FakeMessage(topic=topic, value=raw, key=key.encode() if key else None, partition=partition, offset=offset)


class FakeConsumer:
    """A consumer over a fixed list of messages. `commits` records (topic, partition, offset) in order; `on_commit`
    runs at each commit so tests can look at the database at that moment."""

    def __init__(self, messages: list[FakeMessage], on_commit: Callable | None = None) -> None:
        self._queue = list(messages)
        self.commits: list[tuple[str, int, int]] = []
        self.on_commit = on_commit
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def next_message(self, timeout: float) -> FakeMessage | None:
        if not self._queue:
            await asyncio.sleep(min(timeout, 0.02))
            return None
        return self._queue.pop(0)

    async def commit_message(self, msg: FakeMessage) -> None:
        if self.on_commit is not None:
            await self.on_commit(msg)
        self.commits.append((msg.topic, msg.partition, msg.offset))

    @property
    def drained(self) -> bool:
        return not self._queue


class FakeEmailSender:
    """Records the emails an invite job would send (never sends anything). `fail_times` makes the first N sends raise."""

    def __init__(self) -> None:
        self.sent: list = []
        self.attempts = 0
        self.fail_times = 0

    async def send(self, message) -> None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("smtp unavailable")
        self.sent.append(message)

    def tokens(self) -> list[str]:
        """The invite tokens in the links of the emails sent so far (what a recipient would click)."""
        import re
        return [m.group(1) for e in self.sent for m in [re.search(r"/join/([A-Za-z0-9_-]+)", e.text)] if m]


class FakeDns:
    """DnsClient for tests: no real DNS. `records[name]` is a list of TXT records, each a list of character-strings (the
    pieces a long TXT value is split into); `errors` names that time out; `calls` is every name looked up."""

    def __init__(self) -> None:
        self.records: dict[str, list[list[str]]] = {}
        self.errors: set[str] = set()
        self.calls: list[str] = []

    def publish(self, domain: str, token: str, *, split: bool = False, extra: tuple[str, ...] = ()) -> None:
        value = f"granthan-verify={token}"
        pieces = [value[:10], value[10:25], value[25:]] if split else [value]
        self.records[domain] = [list(extra and [e] or ["v=spf1 -all"]) for e in extra] + [pieces]

    def clear(self, domain: str) -> None:
        self.records.pop(domain, None)

    async def txt(self, name: str):
        from app.clients.dns_client import TxtLookup
        self.calls.append(name)
        if name in self.errors:
            return TxtLookup(error=True)
        return TxtLookup(["".join(parts) for parts in self.records.get(name, [])])
