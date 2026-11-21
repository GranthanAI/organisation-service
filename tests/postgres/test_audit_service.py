"""audit_service.record: actor kinds, correlation id, forbidden keys, size cap, written in the caller's transaction."""

import uuid

import pytest
from sqlalchemy import func, select

from app.core import constants
from app.core.clock import FixedClock
from app.db.session import unit_of_work
from app.middleware.correlation import _correlation_id_ctx
from app.models.audit_log import AuditLog
from app.repositories.audit_repository import AuditRepository
from app.repositories.org_repository import OrgRepository
from app.security.models import CurrentUser, ServiceCaller
from app.services import audit_service
from tests.support.factories import NOW, make_org


class Boom(Exception):
    pass


def user(**kw):
    return CurrentUser(id=uuid.uuid4(), email="a@amity.edu", jti="j", **kw)


async def org(session_factory):
    o = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(o)
    return o


async def count(session_factory) -> int:
    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(AuditLog))).scalar_one()


async def only_row(session_factory) -> AuditLog:
    async with session_factory() as s:
        return (await s.execute(select(AuditLog))).scalar_one()


async def test_a_user_actor(session_factory):
    o, u = await org(session_factory), user()
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, u, constants.AUDIT_MEMBER_REMOVED, "membership", uuid.UUID(int=5),
                                   {"role": "member"}, "left the course", org_id=o.id, clock=FixedClock(NOW))
    row = await only_row(session_factory)
    assert (row.actor_id, row.actor_kind, row.action) == (u.id, "user", "member.removed")
    assert (row.org_id, row.target_type, row.target_id, row.reason) == (o.id, "membership", str(uuid.UUID(int=5)),
                                                                        "left the course")
    assert row.details == {"role": "member"} and row.at == NOW


async def test_a_staff_actor_through_an_admin_route(session_factory):
    o, u = await org(session_factory), user(roles=["admin"])
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, u, constants.AUDIT_SEATS_SET, "org", o.id, {"seats": 50}, "sold 50 seats",
                                   org_id=o.id, staff=True)
    row = await only_row(session_factory)
    assert row.actor_kind == "staff" and row.actor_id == u.id


async def test_a_service_actor_records_its_name_and_no_actor_id(session_factory):
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, ServiceCaller("billing"), constants.AUDIT_ORG_STATUS_CHANGED, "org", "x",
                                   {"to": "active"})
    row = await only_row(session_factory)
    assert row.actor_kind == "service" and row.actor_id is None
    assert row.details == {"to": "active", "service": "billing"}


async def test_the_system_actor(session_factory):
    o = await org(session_factory)
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, "system", constants.AUDIT_ORG_OWNER_MISSING, "org", o.id, org_id=o.id)
    row = await only_row(session_factory)
    assert row.actor_kind == "system" and row.actor_id is None and row.details == {} and row.reason is None


def test_an_unknown_actor_type_is_refused():
    with pytest.raises(TypeError):
        audit_service.actor_fields("somebody", False)
    with pytest.raises(TypeError):
        audit_service.actor_fields(42, False)  # type: ignore[arg-type]


async def test_the_correlation_id_is_stamped_from_the_context_var(session_factory):
    token = _correlation_id_ctx.set("cid-audit-1")
    try:
        async with unit_of_work(session_factory) as s:
            await audit_service.record(s, "system", constants.AUDIT_ORG_CREATED, "org", None)
    finally:
        _correlation_id_ctx.reset(token)
    assert (await only_row(session_factory)).correlation_id == "cid-audit-1"


async def test_without_a_request_the_correlation_id_is_null(session_factory):
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, "system", constants.AUDIT_ORG_CREATED, "org", None)
    assert (await only_row(session_factory)).correlation_id is None


@pytest.mark.parametrize("key", ["token", "access_token", "Secret", "client_secret", "password", "new_password_hash",
                                 "invite_token_raw"])
async def test_forbidden_keys_are_refused_at_any_depth(session_factory, key):
    async with unit_of_work(session_factory) as s:
        with pytest.raises(ValueError, match="may not contain a key"):
            await audit_service.record(s, "system", "x", None, None, {key: "v"})
        with pytest.raises(ValueError):
            await audit_service.record(s, "system", "x", None, None, {"ok": {"nested": [{key: "v"}]}})
    assert await count(session_factory) == 0


async def test_innocent_keys_pass(session_factory):
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, "system", "x", None, None, {"email": "a@b.co", "seats": 3, "from": "pilot", "to": "active"})
    assert await count(session_factory) == 1


async def test_details_must_be_json_serialisable(session_factory):
    async with unit_of_work(session_factory) as s:
        for bad in ({"when": NOW}, {"ids": {uuid.uuid4()}}, {"obj": object()}):
            with pytest.raises(ValueError, match="JSON"):
                await audit_service.record(s, "system", "x", None, None, bad)


async def test_details_over_8_kb_are_refused_and_just_under_pass(session_factory):
    limit = constants.AUDIT_DETAILS_MAX_BYTES
    assert limit == 8192
    overhead = len('{"note": ""}')
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, "system", "x", None, None, {"note": "a" * (limit - overhead)})   # exactly 8 KB
        with pytest.raises(ValueError, match="larger than"):
            await audit_service.record(s, "system", "x", None, None, {"note": "a" * (limit - overhead + 1)})
        with pytest.raises(ValueError):
            await audit_service.record(s, "system", "x", None, None, {"note": "é" * limit})   # bytes, not characters
    assert await count(session_factory) == 1


async def test_the_row_rolls_back_with_the_callers_transaction(session_factory):
    o = await org(session_factory)
    with pytest.raises(Boom):
        async with unit_of_work(session_factory) as s:
            await audit_service.record(s, "system", constants.AUDIT_MEMBER_JOINED, "membership", None, org_id=o.id)
            await s.flush()
            raise Boom
    assert await count(session_factory) == 0
    async with unit_of_work(session_factory) as s:
        await audit_service.record(s, "system", constants.AUDIT_MEMBER_JOINED, "membership", None, org_id=o.id)
    assert await count(session_factory) == 1


async def test_record_never_commits_on_its_own(session_factory):
    async with session_factory() as s:
        await audit_service.record(s, "system", constants.AUDIT_ORG_CREATED, "org", None)
        await s.rollback()                      # the caller owns the transaction
    assert await count(session_factory) == 0


async def test_entries_read_back_newest_first_through_the_repository(session_factory):
    o = await org(session_factory)
    clock = FixedClock(NOW)
    async with unit_of_work(session_factory) as s:
        for action in (constants.AUDIT_ORG_CREATED, constants.AUDIT_SEATS_SET):
            await audit_service.record(s, "system", action, "org", o.id, org_id=o.id, clock=clock)
            clock.advance(minutes=1)
    async with session_factory() as s:
        assert [r.action for r in await AuditRepository(s).list_page(o.id)] == ["seats.set", "org.created"]


def test_the_action_names_are_constants():
    assert constants.AUDIT_ACTIONS == {
        "member.joined", "member.left", "member.removed", "member.role_changed", "org.created", "org.status_changed",
        "seats.set", "invite.created", "invite.revoked", "domain.verified", "policy.changed", "org.owner_missing",
        "org.details_changed", "org.ownership_transferred", "invite.resent",
    }
