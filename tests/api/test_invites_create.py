"""POST /org/v1/orgs/{org_id}/invites: per-email results, who may invite whom, limits, seats warning, no raw token anywhere."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.db.session import unit_of_work
from app.models.audit_log import AuditLog
from app.models.invite import Invite
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.repositories.known_user_repository import KnownUserRepository
from app.security.tokens import invite_token_hash
from tests.support.factories import NOW, make_invite


def url(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/invites"


async def invite(api, world, emails, role="member", who=None, key=None):
    return await api.post(url(world), {"emails": emails, "role": role}, api.h(who or world.owner), key=key)


async def all_rows(api, model):
    async with api.sf() as s:
        return list((await s.execute(select(model))).scalars())


# ---------------------------------------------------------------- the per-email results
async def test_a_new_email_is_created_with_a_job_an_event_and_an_audit_row(api):
    world = await api.seed_org()
    r = await invite(api, world, ["New.Student@Amity.edu"])
    assert r.status_code == 200
    (res,) = r.json()["results"]
    assert res["email"] == "new.student@amity.edu" and res["result"] == "created" and uuid.UUID(res["invite_id"])

    (inv,) = await all_rows(api, Invite)
    assert (inv.kind, inv.role, inv.email, inv.created_by, inv.token_version, inv.resend_count) == (
        "email", "member", "new.student@amity.edu", world.owner.id, 1, 0)
    assert inv.expires_at == NOW + timedelta(days=settings.INVITE_TTL_DAYS) and inv.token_hash == invite_token_hash(inv.id, 1)
    (job,) = [j for j in await all_rows(api, Job) if j.kind == "invite.send"]
    assert job.payload == {"invite_id": str(inv.id), "token_version": 1}
    (event,) = await all_rows(api, OutboxEvent)
    assert event.topic == "org.invite.created" and event.key == str(world.org.id)
    assert event.payload["email"] == "new.student@amity.edu" and event.payload["role"] == "member"
    assert event.payload["org_name"] == world.org.name and event.payload["inviter_name"] == world.owner.name
    (audit,) = [a for a in await all_rows(api, AuditLog) if a.action == "invite.created"]
    assert audit.details == {"email": "new.student@amity.edu", "role": "member"} and audit.actor_id == world.owner.id


async def test_every_other_result(api):
    world = await api.seed_org()
    other = await api.seed_org()
    async with unit_of_work(api.sf) as s:
        await api.person(s, world.org.id, "member", 90, name="Already Here")        # already a member of THIS org
    mine = world.members[0]
    elsewhere = other.members[0]
    async with unit_of_work(api.sf) as s:
        s.add(make_invite(world.org.id, email="open@amity.edu", token_hash="h-open"))   # an open invite exists
        s.add(make_invite(world.org.id, email="stale@amity.edu", token_hash="h-stale", expires_at=NOW - timedelta(days=1)))
    r = await invite(api, world, ["not-an-email", mine.email, elsewhere.email, "open@amity.edu", "stale@amity.edu",
                                  "fresh@amity.edu", "Fresh@amity.edu"])
    results = {x["email"]: x["result"] for x in r.json()["results"]}
    assert results["not-an-email"] == "invalid_email"
    assert results[mine.email] == "already_member"
    assert results[elsewhere.email] == "in_another_org"                    # never says which org
    assert results["open@amity.edu"] == "open_invite_exists"
    assert results["stale@amity.edu"] == "created"                         # the expired open invite was closed first (H4)
    assert results["fresh@amity.edu"] in ("created", "duplicate_in_request")
    assert [x["result"] for x in r.json()["results"]].count("duplicate_in_request") == 1
    assert other.org.name not in r.text and str(other.org.id) not in r.text
    async with api.sf() as s:
        stale = (await s.execute(select(Invite).where(Invite.token_hash == "h-stale"))).scalar_one()
    assert stale.revoked_at == NOW and stale.revoke_reason == "expired"


async def test_a_duplicate_in_one_request_creates_one_invite(api):
    world = await api.seed_org()
    r = await invite(api, world, ["a@amity.edu", "A@AMITY.EDU", " a@amity.edu "])
    assert [x["result"] for x in r.json()["results"]] == ["created", "duplicate_in_request", "duplicate_in_request"]
    assert len(await all_rows(api, Invite)) == 1


async def test_an_open_invite_blocks_a_second_one_use_resend(api):
    world = await api.seed_org()
    await invite(api, world, ["dup@amity.edu"])
    r = await invite(api, world, ["dup@amity.edu"])
    assert r.json()["results"][0]["result"] == "open_invite_exists" and len(await all_rows(api, Invite)) == 1


async def test_a_response_never_has_a_token_a_hash_or_a_link(api):
    world = await api.seed_org()
    r = await invite(api, world, ["one@amity.edu"])
    inv = (await all_rows(api, Invite))[0]
    assert inv.token_hash not in r.text and "join/" not in r.text and "token" not in r.text.lower()
    assert set(r.json()) == {"results", "seats"} and set(r.json()["results"][0]) == {"email", "result", "invite_id"}


# ---------------------------------------------------------------- who may invite
async def test_an_admin_cannot_invite_an_admin_but_the_owner_can(api):
    world = await api.seed_org()
    r = await invite(api, world, ["boss@amity.edu"], role="admin", who=world.admin)
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ROLE"
    assert (await invite(api, world, ["boss@amity.edu"], role="admin", who=world.owner)).json()["results"][0]["result"] == "created"
    assert (await invite(api, world, ["peer@amity.edu"], role="member", who=world.admin)).json()["results"][0]["result"] == "created"
    roles = {i.email: i.role for i in await all_rows(api, Invite)}
    assert roles == {"boss@amity.edu": "admin", "peer@amity.edu": "member"}


async def test_a_member_cannot_invite_and_a_stranger_gets_404(api):
    world = await api.seed_org()
    assert (await invite(api, world, ["x@amity.edu"], who=world.member)).status_code == 403
    assert (await api.post(url(world), {"emails": ["x@amity.edu"]}, api.h(uuid.uuid4()))).status_code == 404
    assert (await api.client.post(url(world), json={"emails": ["x@amity.edu"]},
                                  headers={"Idempotency-Key": uuid.uuid4().hex})).status_code == 401
    assert await all_rows(api, Invite) == []


@pytest.mark.parametrize("role", ["owner", "boss", ""])
async def test_only_member_and_admin_roles_can_be_invited(api, role):
    world = await api.seed_org()
    assert (await invite(api, world, ["x@amity.edu"], role=role)).status_code == 422


@pytest.mark.parametrize("emails", [[], ["a@amity.edu"] * 0, [f"u{i}@amity.edu" for i in range(51)]])
async def test_between_1_and_50_emails(api, emails):
    world = await api.seed_org()
    assert (await invite(api, world, emails)).status_code == 422


async def test_exactly_50_emails_are_fine(api):
    world = await api.seed_org(seats=500)
    r = await invite(api, world, [f"person{i}@amity.edu" for i in range(50)])
    assert r.status_code == 200 and [x["result"] for x in r.json()["results"]].count("created") == 50


# ---------------------------------------------------------------- limits
async def test_the_daily_limit_gives_429_and_nothing_is_created(api, monkeypatch):
    monkeypatch.setattr(settings, "INVITES_PER_ORG_PER_DAY", 5)
    world = await api.seed_org(seats=500)
    assert (await invite(api, world, [f"a{i}@amity.edu" for i in range(5)])).status_code == 200
    r = await invite(api, world, ["one-more@amity.edu"])
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and int(r.headers["retry-after"]) > 0
    assert len(await all_rows(api, Invite)) == 5


async def test_a_request_that_would_cross_the_limit_is_refused_whole(api, monkeypatch):
    monkeypatch.setattr(settings, "INVITES_PER_ORG_PER_DAY", 5)
    world = await api.seed_org(seats=500)
    await invite(api, world, [f"a{i}@amity.edu" for i in range(3)])
    r = await invite(api, world, [f"b{i}@amity.edu" for i in range(4)])        # 3 + 4 > 5
    assert r.status_code == 429
    assert len(await all_rows(api, Invite)) == 3 and len([j for j in await all_rows(api, Job) if j.kind == "invite.send"]) == 3


async def test_the_day_is_utc_and_resets_at_midnight(api, monkeypatch):
    monkeypatch.setattr(settings, "INVITES_PER_ORG_PER_DAY", 2)
    world = await api.seed_org(seats=500)
    assert (await invite(api, world, ["a@amity.edu", "b@amity.edu"])).status_code == 200
    assert (await invite(api, world, ["c@amity.edu"])).status_code == 429
    api.clock.set(NOW + timedelta(days=1))                                      # NOW is 12:00 UTC: next day
    assert (await invite(api, world, ["c@amity.edu"])).status_code == 200


async def test_invalid_and_existing_emails_do_not_use_up_the_limit(api, monkeypatch):
    monkeypatch.setattr(settings, "INVITES_PER_ORG_PER_DAY", 2)
    world = await api.seed_org(seats=500)
    await invite(api, world, ["bad", "also bad", world.member.email, "a@amity.edu"])
    assert (await invite(api, world, ["b@amity.edu"])).status_code == 200       # only one invite counted so far


async def test_the_per_user_rate_limit_bucket_applies(api):
    from app.core.constants import RATE_INVITE_CREATE
    world = await api.seed_org(seats=500)
    codes = [(await invite(api, world, [f"p{i}@amity.edu"])).status_code for i in range(RATE_INVITE_CREATE.limit + 1)]
    assert codes[:RATE_INVITE_CREATE.limit] == [200] * RATE_INVITE_CREATE.limit and codes[-1] == 429


# ---------------------------------------------------------------- seats and the status gate
async def test_the_seat_warning_only_informs(api):
    world = await api.seed_org(plan_code="org_pro", seats=6, admins=1, members=3)            # 5 used, 1 free seat
    ok = (await invite(api, world, ["a@amity.edu"])).json()["seats"]
    assert ok == {"used": 5, "bought": 6, "open_invites": 1, "warning": False}
    over = await invite(api, world, ["b@amity.edu", "c@amity.edu"])
    assert over.status_code == 200 and over.json()["seats"] == {"used": 5, "bought": 6, "open_invites": 3, "warning": True}
    assert [x["result"] for x in over.json()["results"]] == ["created", "created"]            # created anyway: accept enforces


async def test_payg_and_incomplete_seats_never_warn(api):
    payg = await api.seed_org(plan_code="payg", seats=None)
    assert (await invite(api, payg, ["a@amity.edu"])).json()["seats"]["warning"] is False
    incomplete = await api.seed_org(seats=None, seat_status="incomplete")
    seats = (await invite(api, incomplete, ["a@amity.edu"])).json()["seats"]
    assert seats["bought"] is None and seats["warning"] is False


@pytest.mark.parametrize("status, ok", [("pilot", True), ("active", True), ("suspended", False), ("pilot_expired", False),
                                        ("closed", False)])
async def test_only_a_pilot_or_active_org_may_invite(api, status, ok):
    world = await api.seed_org(status=status)
    r = await invite(api, world, ["a@amity.edu"])
    if ok:
        assert r.status_code == 200
    else:
        assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE" and await all_rows(api, Invite) == []


# ---------------------------------------------------------------- the raw token is never stored
async def test_the_raw_token_is_nowhere_in_the_database(api):
    world = await api.seed_org()
    await invite(api, world, ["secret.one@amity.edu", "secret.two@amity.edu"], role="member")
    assert await api.run_jobs() >= 2                                          # the jobs send the emails after the commit
    tokens = api.mail.tokens()
    assert len(tokens) == 2 and all(len(t) == 43 for t in tokens)
    async with api.sf() as s:
        for table in ("invites", "outbox", "jobs", "audit_log", "idempotency_keys", "known_users", "organisations"):
            dump = (await s.execute(text(f"SELECT t::text FROM {table} t"))).scalars().all()
            blob = "\n".join(dump)
            for token in tokens:
                assert token not in blob, f"the raw token is stored in {table}"
    sent = {m.to for m in api.mail.sent}
    assert sent == {"secret.one@amity.edu", "secret.two@amity.edu"}


async def test_the_outbox_event_has_no_token_and_no_url(api):
    world = await api.seed_org()
    await invite(api, world, ["evt@amity.edu"])
    (event,) = await all_rows(api, OutboxEvent)
    flat = str(event.payload)
    assert "token" not in flat.lower() and "http" not in flat and "/join/" not in flat and "accept_url" not in event.payload
    assert set(event.payload) == {"event_id", "schema_version", "occurred_at", "org_id", "invite_id", "email", "role", "org_name",
                                  "inviter_name", "expires_at"}


async def test_notification_mode_queues_no_email_job_but_still_publishes_the_event(api, monkeypatch):
    monkeypatch.setattr(settings, "INVITE_EMAILS_VIA", "notification")
    world = await api.seed_org()
    await invite(api, world, ["n@amity.edu"])
    assert [j for j in await all_rows(api, Job) if j.kind == "invite.send"] == []
    assert len(await all_rows(api, OutboxEvent)) == 1
    assert await api.run_jobs() == 0 and api.mail.sent == []


async def test_a_replayed_request_creates_once(api):
    world = await api.seed_org()
    first = await invite(api, world, ["once@amity.edu"], key="invite-key-0001")
    second = await invite(api, world, ["once@amity.edu"], key="invite-key-0001")
    assert first.json() == second.json() and second.headers["idempotent-replayed"] == "true"
    assert len(await all_rows(api, Invite)) == 1 and len(await all_rows(api, OutboxEvent)) == 1


async def test_an_invited_known_user_with_no_membership_is_still_invitable(api):
    world = await api.seed_org()
    stranger = uuid.uuid4()
    async with unit_of_work(api.sf) as s:
        await KnownUserRepository(s).upsert(stranger, "free.agent@amity.edu", "Free Agent", NOW)
    assert (await invite(api, world, ["free.agent@amity.edu"])).json()["results"][0]["result"] == "created"
