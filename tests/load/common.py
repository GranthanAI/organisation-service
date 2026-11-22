"""Helpers for the load scripts (run against a RUNNING service started with `make dev`; they are scripts, not pytest tests).

They write test data straight into the dev database (the same DATABASE_URL as the service) and talk to the service over HTTP only.
Development only: they refuse to run when ENVIRONMENT is production."""

import time
import uuid

import jwt

from app.core.config import settings
from app.core.enums import Role
from app.db.session import unit_of_work
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.seat_state import SeatState
from app.repositories.known_user_repository import KnownUserRepository
from app.utils.ids import uuid7

if settings.is_production:
    raise SystemExit("the load scripts are for development only")


def token(user_id: uuid.UUID, email: str, minutes: int = 60) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id), "email": email, "roles": [], "jti": uuid.uuid4().hex, "iat": now, "exp": now + minutes * 60,
              "iss": settings.JWT_ISSUER}
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def bearer(user_id: uuid.UUID, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id, email)}"}


async def seed_org(seats: int, tag: str, org_type: str = "college") -> tuple[uuid.UUID, uuid.UUID, str]:
    """An active org with one owner and `seats` seats bought. Returns (org_id, owner_id, owner_email)."""
    from app.core.clock import system_clock
    now = system_clock.now()
    org_id, owner = uuid7(), uuid7()
    email = f"owner-{tag}@load.test"
    async with unit_of_work() as s:
        s.add(Organisation(id=org_id, name=f"Load {tag}", slug=f"load-{tag}", type=org_type, status="active", created_at=now, updated_at=now))
        await s.flush()
        s.add(SeatState(org_id=org_id, plan_code="org_pro", variant=org_type, seats_bought=seats, status="active", source="staff", refreshed_at=now))
        await KnownUserRepository(s).upsert(owner, email, "Load Owner", now)
        s.add(Membership(id=uuid7(), org_id=org_id, user_id=owner, role=Role.OWNER.value, status="active", source="staff", joined_at=now))
    return org_id, owner, email
