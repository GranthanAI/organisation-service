"""Seed one college organisation for the live check: an owner and a member, both known users with verified emails,
a pilot seat_state, and an open invite to the member's email. Prints the ids as JSON.

    uv run python -m scripts.seed_dev            # new ids every run (slug is random)

Uses DATABASE_URL (the dev database). Never run against production: it is a development helper.
"""

import asyncio
import json
import uuid

from app.core.clock import system_clock
from app.core.config import settings
from app.db.session import unit_of_work
from app.models.invite import Invite
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.seat_state import SeatState
from app.repositories.known_user_repository import KnownUserRepository
from app.utils.ids import uuid7


async def main() -> dict:
    if settings.is_production:
        raise SystemExit("seed_dev.py is for development only")
    now = system_clock.now()
    org_id, owner, member = uuid7(), uuid7(), uuid7()
    async with unit_of_work() as s:
        s.add(Organisation(id=org_id, name="Seed College", slug=f"seed-college-{uuid.uuid4().hex[:8]}", type="college",
                           status="pilot", created_at=now, updated_at=now))
        await s.flush()
        s.add(SeatState(org_id=org_id, plan_code="pilot", variant="college", seats_bought=25, status="active",
                        source="staff", refreshed_at=now))
        users = KnownUserRepository(s)
        for user_id, email, name in ((owner, "seed.owner@seed.edu", "Seed Owner"),
                                     (member, "seed.member@seed.edu", "Seed Member")):
            await users.upsert(user_id, email, name, now)
            await users.set_email_verified(user_id, email, now, now)
        s.add(Membership(id=uuid7(), org_id=org_id, user_id=owner, role="owner", status="active", source="staff",
                         joined_at=now))
        s.add(Membership(id=uuid7(), org_id=org_id, user_id=member, role="member", status="active", source="staff",
                         joined_at=now))
        s.add(Invite(id=uuid7(), org_id=org_id, kind="email", email="seed.member@seed.edu", role="member",
                     token_hash=uuid.uuid4().hex + uuid.uuid4().hex, expires_at=now.replace(year=now.year + 1),
                     created_at=now))
    return {"org_id": str(org_id), "owner_id": str(owner), "member_id": str(member)}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
