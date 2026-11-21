"""require_org_role on a test-only app: the matrix per role, 404 (never 403) for non-members and ended members."""

import uuid

import fakeredis
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db_session, get_redis_client
from app.core.enums import MembershipStatus, Role, StaffRole
from app.db.session import unit_of_work
from app.middleware.correlation import CorrelationIDMiddleware
from app.middleware.exception_handler import register_exception_handlers
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.security.dependencies import require_org_role, require_staff
from app.security.models import CurrentUser, OrgContext
from app.services.permission_rules import Action, Permission, permission_for
from tests.support.factories import NOW, make_membership, make_org
from tests.support.tokens import bearer, make_token


def build_app(session_factory) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIDMiddleware)
    register_exception_handlers(app)

    async def session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_redis_client] = lambda: fakeredis.FakeAsyncRedis()

    def add(action: Action):
        @app.get(f"/t/orgs/{{org_id}}/{action.value}")
        async def route(ctx: OrgContext = Depends(require_org_role(action))):
            return {"org_id": str(ctx.org_id), "user": str(ctx.user.id), "role": ctx.role.value,
                    "membership_id": str(ctx.membership_id)}

    for action in Action:
        add(action)

    @app.get("/t/staff-only")
    async def staff_only(user: CurrentUser = Depends(require_staff(StaffRole.SUPPORT))):
        return {"ok": True}

    @app.get("/t/admin-only")
    async def admin_only(user: CurrentUser = Depends(require_staff(StaffRole.ADMIN))):
        return {"ok": True}

    return app


@pytest.fixture
async def world(session_factory):
    """One org with an owner, an admin and a member; a second org; and people with no or ended membership."""
    org, other = make_org(), make_org()
    people = {name: uuid.uuid4() for name in ("owner", "admin", "member", "ended", "outsider", "other_org")}
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await OrgRepository(s).add(other)
        repo = MembershipRepository(s)
        for role in ("owner", "admin", "member"):
            await repo.add(make_membership(org.id, people[role], role=role))
        await repo.add(make_membership(org.id, people["ended"], role="admin", status=MembershipStatus.LEFT.value,
                                       ended_at=NOW, end_reason="left"))
        await repo.add(make_membership(other.id, people["other_org"], role="owner"))
    app = build_app(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, org, other, people


def headers_for(user: uuid.UUID, **kw):
    return bearer(make_token(sub=user, **kw))


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.MEMBER])
async def test_each_member_role_against_every_action(world, role):
    client, org, _other, people = world
    for action in Action:
        r = await client.get(f"/t/orgs/{org.id}/{action.value}", headers=headers_for(people[role.value]))
        permission = permission_for(action, role)
        if permission is Permission.DENIED:
            assert r.status_code == 403, (action, role)       # a member knows the org exists: 403, not 404
            assert r.json()["code"] == "FORBIDDEN_ROLE", (action, role)
        else:
            # ALLOWED, and OWN_ROLE_ONLY (the use case checks the target's role)
            assert r.status_code == 200, (action, role)
            assert r.json()["role"] == role.value and r.json()["org_id"] == str(org.id)


async def test_the_context_carries_org_user_role_and_membership(world):
    client, org, _other, people = world
    r = await client.get(f"/t/orgs/{org.id}/view_org", headers=headers_for(people["admin"]))
    body = r.json()
    assert body["user"] == str(people["admin"]) and body["role"] == "admin"
    assert uuid.UUID(body["membership_id"])


@pytest.mark.parametrize("who", ["outsider", "ended", "other_org"])
async def test_non_members_and_ended_members_get_404_never_403(world, who):
    client, org, _other, people = world
    for action in Action:   # every action, including the staff-only ones
        r = await client.get(f"/t/orgs/{org.id}/{action.value}", headers=headers_for(people[who]))
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND", (who, action)
        assert r.headers["content-type"].startswith("application/problem+json")


async def test_a_missing_org_looks_the_same_as_someone_elses_org(world):
    client, org, _other, people = world
    missing = await client.get(f"/t/orgs/{uuid.uuid4()}/view_org", headers=headers_for(people["owner"]))
    foreign = await client.get(f"/t/orgs/{org.id}/view_org", headers=headers_for(people["other_org"]))
    assert missing.status_code == foreign.status_code == 404
    assert {k: v for k, v in missing.json().items() if k not in ("correlation_id",)} == \
           {k: v for k, v in foreign.json().items() if k not in ("correlation_id",)}


async def test_staff_do_not_pass_through_require_org_role(world):
    client, org, _other, people = world
    r = await client.get(f"/t/orgs/{org.id}/view_org",
                         headers=headers_for(uuid.uuid4(), roles=["admin", "billing_admin", "support"]))
    assert r.status_code == 404   # staff use the admin routes


async def test_no_token_is_401_before_any_lookup(world):
    client, org, *_ = world
    assert (await client.get(f"/t/orgs/{org.id}/view_org")).status_code == 401


async def test_a_malformed_org_id_is_a_validation_error(world):
    client, _org, _other, people = world
    r = await client.get("/t/orgs/not-a-uuid/view_org", headers=headers_for(people["owner"]))
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


async def test_require_staff_levels(world):
    client, *_ = world
    plain = await client.get("/t/staff-only", headers=headers_for(uuid.uuid4()))
    billing_only = await client.get("/t/staff-only", headers=headers_for(uuid.uuid4(), roles=["billing_admin"]))
    support = await client.get("/t/staff-only", headers=headers_for(uuid.uuid4(), roles=["support"]))
    admin = await client.get("/t/staff-only", headers=headers_for(uuid.uuid4(), roles=["admin"]))
    assert (plain.status_code, billing_only.status_code) == (403, 403) and plain.json()["code"] == "FORBIDDEN_ROLE"
    assert (support.status_code, admin.status_code) == (200, 200)
    only_support = await client.get("/t/admin-only", headers=headers_for(uuid.uuid4(), roles=["support"]))
    only_admin = await client.get("/t/admin-only", headers=headers_for(uuid.uuid4(), roles=["admin"]))
    assert only_support.status_code == 403 and only_admin.status_code == 200
