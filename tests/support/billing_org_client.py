"""COPIED from billing-service (commit 11a5fed), src/billing/adapters/orgs.py: `_membership`, `_org`, `_get` and the
`HttpOrgDirectory` methods that call organisation-service. DO NOT EDIT the parsing: if billing changes, copy it again.

Only the imports differ: billing's records and enum are re-declared below with the same fields, and the caching
wrapper (Valkey) is left out. This is what billing really does with our responses: ANY 404 means "no org"; a 4xx other
than 404 raises; a 5xx or a dropped connection is retried once and then raises.
"""

import enum
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

TIMEOUT_SECONDS = 2.0


class OrgDirectoryUnavailable(Exception):
    pass


class OrgVariant(str, enum.Enum):
    COLLEGE = "college"
    COMPANY = "company"


@dataclass(frozen=True)
class Membership:
    org_id: UUID
    org_type: OrgVariant
    role: str
    status: str


@dataclass(frozen=True)
class OrgInfo:
    org_id: UUID
    name: str
    type: OrgVariant
    gstin: str | None
    billing_email: str | None
    state_code: str | None
    member_spend_cap_paise: int | None


# ---- copied from billing-service ------------------------------------------------------------------------------
def _membership(d: dict[str, Any]) -> Membership:
    return Membership(
        org_id=UUID(d["org_id"]),
        org_type=OrgVariant(d["org_type"]),
        role=str(d.get("role", "member")),
        status=str(d.get("status", "active")),
    )


def _org(org_id: UUID, d: dict[str, Any]) -> OrgInfo:
    cap = d.get("member_spend_cap_paise")
    return OrgInfo(
        org_id=org_id,
        name=str(d["name"]),
        type=OrgVariant(d["type"]),
        gstin=d.get("gstin") or None,
        billing_email=d.get("billing_email") or None,
        state_code=d.get("state_code") or None,
        member_spend_cap_paise=int(cap) if cap is not None else None,
    )


class HttpOrgDirectory:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str) -> dict[str, Any] | None:
        last = ""
        for _ in range(2):  # one retry for a dropped connection
            try:
                resp = await self._http.get(path)
            except httpx.TransportError as exc:
                last = type(exc).__name__
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code >= 400:
                raise OrgDirectoryUnavailable(
                    f"org-service refused {path}: HTTP {resp.status_code}"
                )
            body: dict[str, Any] = resp.json()
            return body
        raise OrgDirectoryUnavailable(f"org-service unavailable for {path}: {last}")

    async def membership(self, user_id: UUID) -> Membership | None:
        found = await self._get(f"/internal/memberships/{user_id}")
        return _membership(found) if found else None

    async def org(self, org_id: UUID) -> OrgInfo | None:
        found = await self._get(f"/internal/orgs/{org_id}")
        return _org(org_id, found) if found else None

    async def members(self, org_id: UUID) -> list[UUID]:
        found = await self._get(f"/internal/orgs/{org_id}/members")
        return [UUID(u) for u in (found or {}).get("user_ids", [])]
