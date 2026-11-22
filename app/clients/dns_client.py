"""DNS TXT lookups behind a port. `DnsPythonClient` (dnspython, async, 3 s) in production, `FakeDns` in tests.

A lookup answers `TxtLookup(records, error)`: `records` are the TXT records of the name, each one's character-strings
already joined (a long TXT value is split in 255-byte pieces on the wire); `error` is True only for a timeout or SERVFAIL.
NXDOMAIN, "no TXT here" and the like are plain empty results: "not found" is not an error.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import settings
from app.core.logging import logger

TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class TxtLookup:
    records: list[str] = field(default_factory=list)
    error: bool = False


class DnsClient(Protocol):
    async def txt(self, name: str) -> TxtLookup: ...


class DnsPythonClient:
    def __init__(self, nameservers: str = "", timeout: float = TIMEOUT_SECONDS) -> None:
        import dns.asyncresolver

        self._resolver = dns.asyncresolver.Resolver(configure=True)
        self._resolver.lifetime = timeout
        self._resolver.timeout = timeout
        servers = [s.strip() for s in nameservers.split(",") if s.strip()]
        if servers:
            self._resolver.nameservers = servers

    async def txt(self, name: str) -> TxtLookup:
        import dns.exception
        import dns.resolver

        try:
            answer = await self._resolver.resolve(name, "TXT")
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.YXDOMAIN):
            return TxtLookup()
        except dns.resolver.NoNameservers:                       # every server answered SERVFAIL (or refused)
            logger.warning("dns lookup failed (servfail)", name=name)
            return TxtLookup(error=True)
        except dns.exception.Timeout:
            logger.warning("dns lookup timed out", name=name)
            return TxtLookup(error=True)
        except dns.exception.DNSException as exc:
            logger.warning("dns lookup failed", name=name, error=type(exc).__name__)
            return TxtLookup(error=True)
        return TxtLookup([b"".join(rdata.strings).decode("utf-8", "replace") for rdata in answer])


def get_dns_client() -> DnsClient:
    return DnsPythonClient(settings.DNS_NAMESERVERS)
