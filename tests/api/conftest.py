"""Helpers for the /org/v1 API tests: the real app with its dependencies pointed at the throwaway database, fakeredis and a
FixedClock, plus a small world builder (an organisation with an owner, admins and members who are known users)."""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_clock, get_db_session, get_internal_session, get_redis_client, get_session_factory_dep
from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.main import app
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.repositories.seat_state_repository import SeatStateRepository
from app.services import invite_jobs
from app.services.idempotency_service import IdempotencyService
from app.workers.job_runner import JobRunner
from tests.support.factories import NOW, make_membership, make_org, make_seat_state
from tests.support.fakes import FakeEmailSender
from tests.support.tokens import bearer, make_token


@dataclass
class Person:
    id: uuid.UUID
    email: str
    name: str
    role: str


@dataclass
class World:
    org: object
    owner: Person
    admins: list[Person] = field(default_factory=list)
    members: list[Person] = field(default_factory=list)

    @property
    def admin(self) -> Person:
        return self.admins[0]

    @property
    def member(self) -> Person:
        return self.members[0]


class Api:
    def __init__(self, client: AsyncClient, session_factory, clock: FixedClock, mail: FakeEmailSender) -> None:
        self.client, self.sf, self.clock, self.mail = client, session_factory, clock, mail
        self._n = 0

    async def run_jobs(self) -> int:
        """Run what is due (the invite.send jobs queued by a request): the email goes to `self.mail`, never to a network."""
        return await JobRunner(self.sf, None, self.clock, poll_seconds=0.05).run_once()

    def h(self, person: Person | uuid.UUID, email: str | None = None, **kw) -> dict:
        user_id = person.id if isinstance(person, Person) else person
        mail = person.email if isinstance(person, Person) else (email or f"{user_id.hex[:8]}@amity.edu")
        return bearer(make_token(sub=user_id, email=mail, **kw))

    async def post(self, path, json=None, headers=None, key=None):
        return await self.client.post(path, json=json, headers={**(headers or {}), "Idempotency-Key": key or uuid.uuid4().hex})

    async def person(self, session, org_id, role: str, joined_offset: int, name: str | None = None, status="active"):
        self._n += 1
        user_id = uuid.uuid4()
        name = name or f"{role.title()} {self._n}"
        email = f"{role}{self._n}@amity.edu"
        await KnownUserRepository(session).upsert(user_id, email, name, NOW)
        await MembershipRepository(session).add(make_membership(
            org_id, user_id, role=role, joined_at=NOW + timedelta(seconds=joined_offset),
            **({} if status == "active" else {"status": status, "ended_at": NOW, "end_reason": "left"})))
        return Person(user_id, email, name, role)

    async def seed_org(self, *, status="active", type="college", plan_code="org_pro", seats=50, seat_status="active",
                       admins=1, members=3, owner=True, with_seat=True, **org_kw) -> World:
        org = make_org(status=status, type=type, **org_kw)
        async with unit_of_work(self.sf) as s:
            await OrgRepository(s).add(org)
            if with_seat:
                await SeatStateRepository(s).add(make_seat_state(org.id, variant=type, plan_code=plan_code,
                                                                 seats_bought=seats, status=seat_status))
            offset = 0
            world = World(org, None)
            if owner:
                world.owner = await self.person(s, org.id, "owner", offset)
            for _ in range(admins):
                offset += 1
                world.admins.append(await self.person(s, org.id, "admin", offset))
            for _ in range(members):
                offset += 1
                world.members.append(await self.person(s, org.id, "member", offset))
        return world


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
async def api(session_factory, clock, monkeypatch):
    mail = FakeEmailSender()
    monkeypatch.setattr(invite_jobs, "get_email_sender", lambda: mail)
    async def session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_internal_session] = session
    app.dependency_overrides[get_session_factory_dep] = lambda: session_factory
    redis = fakeredis.FakeAsyncRedis()                       # ONE instance: rate-limit counters must persist across requests
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[get_clock] = lambda: clock
    app.state.idempotency_service = IdempotencyService(session_factory, clock)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://org") as client:
        api_obj = Api(client, session_factory, clock, mail)
        api_obj.redis = redis
        yield api_obj
    app.dependency_overrides.clear()
    app.state.idempotency_service = None
