"""A college pilot, end to end, on the dev stack (the PHASES.md Phase 10 exit check, item 3), over HTTP against a running service:

  staff create a college pilot -> the owner uploads a CSV of 20 students -> 20 students accept their links (dev JWTs) ->
  /internal/memberships/{id} shows all 20 as active -> staff move the org to org_pro -> the usage report is asked for from billing.

    uv run python -m tests.load.pilot_end_to_end [--base http://127.0.0.1:8010]

Development only (it writes users straight into the dev database, as the load scripts do).
"""

import argparse
import asyncio
import time
import uuid
from datetime import date

import httpx
import jwt
from sqlalchemy import text

from app.core.clock import system_clock
from app.core.config import settings
from app.db.session import unit_of_work
from app.repositories.known_user_repository import KnownUserRepository
from app.security.tokens import invite_token
from app.utils.ids import uuid7
from tests.load.common import bearer

STUDENTS = 20
SERVICE = {"Authorization": "Bearer dev-billing-token"}


def staff_headers() -> dict[str, str]:
    now = int(time.time())
    claims = {"sub": str(uuid.uuid4()), "email": "staff@granthan.com", "roles": ["admin", "billing_admin", "support"], "jti": uuid.uuid4().hex,
              "iat": now, "exp": now + 3600, "iss": settings.JWT_ISSUER}
    return {"Authorization": "Bearer " + jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)}


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def main(base: str) -> int:
    tag = uuid.uuid4().hex[:6]
    now = system_clock.now()
    owner, owner_email = uuid7(), f"dean-{tag}@pilot.test"
    async with unit_of_work() as s:
        await KnownUserRepository(s).upsert(owner, owner_email, "Dean Of Pilot College", now)
    steps: list[tuple[str, bool, str]] = []

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{base}/admin/org/v1/orgs", json={
            "name": f"Pilot College {tag}", "type": "college", "billing_email": f"accounts-{tag}@pilot.test", "owner_email": owner_email,
            "pilot": True, "reason": "end to end pilot check"}, headers={**staff_headers(), **key()})
        org_id = r.json().get("id")
        steps.append(("staff create the pilot", r.status_code == 201 and r.json().get("owner_joined") is True, f"{r.status_code} owner_joined={r.json().get('owner_joined')}"))
        owner_h = bearer(owner, owner_email)

        emails = [f"student{i:02d}-{tag}@pilot.test" for i in range(STUDENTS)]
        csv_text = "email,name\n" + "".join(f"{e},Student {i}\n" for i, e in enumerate(emails))
        r = await c.post(f"{base}/org/v1/orgs/{org_id}/invites/csv", files={"file": ("students.csv", csv_text.encode(), "text/csv")},
                         headers={**owner_h, **key()})
        steps.append(("owner uploads the CSV of 20", r.status_code == 200 and r.json().get("rows_ok") == STUDENTS, f"{r.status_code} rows_ok={r.json().get('rows_ok')}"))

        students: list[tuple[uuid.UUID, str, str]] = []
        async with unit_of_work() as s:
            for email in emails:
                user = uuid7()
                await KnownUserRepository(s).upsert(user, email, email.split("@")[0], now)
                invite_id = (await s.execute(text("SELECT id FROM invites WHERE org_id = :o AND email = :e"), {"o": org_id, "e": email})).scalar_one()
                students.append((user, email, invite_token(invite_id, 1)))        # the token the email link carries (derived, never stored)

        outcomes = await asyncio.gather(*[c.post(f"{base}/org/v1/invites/accept", json={"token": token}, headers={**bearer(user, email), **key()})
                                          for user, email, token in students])
        steps.append(("20 students accept", all(o.status_code == 200 for o in outcomes), f"{sum(o.status_code == 200 for o in outcomes)} of {STUDENTS} answered 200"))

        looked = [await c.get(f"{base}/internal/memberships/{user}", headers=SERVICE) for user, _, _ in students]
        active = [r.json() for r in looked if r.status_code == 200 and r.json()["status"] == "active" and r.json()["org_id"] == org_id]
        steps.append(("/internal/memberships shows all 20 active", len(active) == STUDENTS and all(a["org_type"] == "college" for a in active),
                      f"{len(active)} active, org_type college"))
        me = await c.get(f"{base}/org/v1/me", headers=owner_h)
        steps.append(("the owner sees 21 members", me.json()["seats"]["used"] == STUDENTS + 1, f"used={me.json()['seats']['used']} bought={me.json()['seats']['bought']}"))

        gated = await c.get(f"{base}/org/v1/orgs/{org_id}/usage", params={"from": date.today().isoformat(), "to": date.today().isoformat()}, headers=owner_h)
        steps.append(("a pilot has no usage report", gated.status_code == 403 and gated.json()["code"] == "FEATURE_NOT_IN_PLAN", f"{gated.status_code} {gated.json().get('code')}"))
        r = await c.post(f"{base}/admin/org/v1/orgs/{org_id}/seats", json={"plan_code": "org_pro", "variant": "college", "seats_bought": 50, "reason": "pilot converted"},
                         headers={**staff_headers(), **key()})
        steps.append(("staff move it to org_pro", r.status_code == 200, f"{r.status_code}"))
        report = await c.get(f"{base}/org/v1/orgs/{org_id}/usage", params={"from": date.today().isoformat(), "to": date.today().isoformat()}, headers=owner_h)
        body = report.json()
        steps.append(("the usage report asks billing", report.status_code in (200, 503),
                      f"{report.status_code} {body.get('code') or ('members=' + str(len(body.get('members', []))))} (200 = real billing answered, 503 = billing unreachable or refused the token)"))

    for name, ok, detail in steps:
        print(("PASS " if ok else "FAIL ") + name + ": " + detail)
    return 0 if all(ok for _, ok, _ in steps) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    raise SystemExit(asyncio.run(main(parser.parse_args().base)))
