"""A round trip per repository against the migrated throwaway database."""

import uuid
from datetime import timedelta

from app.db.session import unit_of_work
from app.models.domain import JoinRequest, OrgDomain
from app.models.invite import InviteBatch
from app.models.job import Job
from app.models.membership import Membership
from app.models.policy import OrgPolicy
from app.repositories import (
    AuditRepository,
    DomainRepository,
    InviteRepository,
    JobRepository,
    KnownUserRepository,
    MembershipRepository,
    OrgRepository,
    OutboxRepository,
    PolicyRepository,
    SeatStateRepository,
)
from app.utils.ids import uuid7
from tests.support.factories import (
    NOW,
    make_audit,
    make_invite,
    make_membership,
    make_org,
    make_outbox,
    make_seat_state,
)


async def new_org(session_factory, **kw):
    org = make_org(**kw)
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
    return org


async def test_org_round_trip_and_search(session_factory):
    org = await new_org(session_factory, name="Amity University", status="pilot", billing_email="Fin@Amity.edu",
                        state_code="09", pilot_ends_at=NOW + timedelta(days=30))
    other = await new_org(session_factory, name="Acme Ltd", slug="acme-ltd", type="company", created_at=NOW + timedelta(hours=1))
    async with unit_of_work(session_factory) as s:
        repo = OrgRepository(s)
        got = await repo.get(org.id)
        assert (got.name, got.status, got.state_code, got.billing_email) == ("Amity University", "pilot", "09",
                                                                             "Fin@Amity.edu")
        assert got.pilot_ends_at == NOW + timedelta(days=30) and got.pilot_ends_at.tzinfo is not None
        assert (await repo.get_by_slug(org.slug)).id == org.id
        assert await repo.get(uuid7()) is None
        assert [o.id for o in await repo.search()] == [other.id, org.id]          # newest first
        assert [o.id for o in await repo.search(q="amity")] == [org.id]
        assert [o.id for o in await repo.search(status="pilot")] == [org.id]
        first = await repo.search(limit=1)
        assert [o.id for o in await repo.search(after=(first[0].created_at, first[0].id))] == [org.id]
    async with unit_of_work(session_factory) as s:
        repo = OrgRepository(s)
        locked = await repo.get_for_update(org.id)
        locked.status = "suspended"
        await repo.save(locked)
    async with unit_of_work(session_factory) as s:
        assert (await OrgRepository(s).get(org.id)).status == "suspended"


async def test_membership_counts_lookups_and_keyset_pages(session_factory):
    org = await new_org(session_factory)
    owner = make_membership(org.id, role="owner", joined_at=NOW)
    users = []
    async with unit_of_work(session_factory) as s:
        repo = MembershipRepository(s)
        await repo.add(owner)
        for i in range(7):  # members joined one minute apart; two share a timestamp to exercise the id tie-break
            m = make_membership(org.id, joined_at=NOW + timedelta(minutes=max(i - 1, 0) + 1))
            users.append(m.user_id)
            await repo.add(m)
        left = make_membership(org.id, status="left", ended_at=NOW, end_reason="left", joined_at=NOW)
        await repo.add(left)
        from app.core.enums import EndReason  # noqa: F401
    async with unit_of_work(session_factory) as s:
        repo = MembershipRepository(s)
        assert await repo.count_active(org.id) == 8
        assert await repo.count_active_owners(org.id) == 1
        assert (await repo.get_active_for_user(owner.user_id)).id == owner.id
        assert await repo.get_active_for_user(left.user_id) is None
        assert (await repo.get_active(org.id, users[0])) is not None
        assert len(await repo.list_active_user_ids(org.id)) == 8
        assert len(await repo.list_active_user_ids(org.id, limit=3)) == 3

        seen, after = [], None
        while True:
            page = await repo.list_page(org.id, after=after, limit=3)
            if not page:
                break
            seen += [m.id for m, _u in page]
            after = (page[-1][0].joined_at, page[-1][0].id)
        assert len(seen) == 8 == len(set(seen))                    # no gaps, no repeats
        assert seen[0] == owner.id                                  # oldest first
        assert [m.role for m, _ in await repo.list_page(org.id, role="owner")] == ["owner"]


async def test_membership_page_is_stable_while_members_join(session_factory):
    org = await new_org(session_factory)
    async with unit_of_work(session_factory) as s:
        for i in range(5):
            await MembershipRepository(s).add(make_membership(org.id, joined_at=NOW + timedelta(minutes=i)))
    async with unit_of_work(session_factory) as s:
        repo = MembershipRepository(s)
        first = await repo.list_page(org.id, limit=3)
    async with unit_of_work(session_factory) as s:  # someone joins between page requests
        await MembershipRepository(s).add(make_membership(org.id, joined_at=NOW + timedelta(hours=1)))
    async with unit_of_work(session_factory) as s:
        repo = MembershipRepository(s)
        rest, after = [], (first[-1][0].joined_at, first[-1][0].id)
        while page := await repo.list_page(org.id, after=after, limit=3):
            rest += page
            after = (page[-1][0].joined_at, page[-1][0].id)
    ids = [m.id for m, _ in first] + [m.id for m, _ in rest]
    assert len(ids) == 6 == len(set(ids))


async def test_membership_search_joins_known_users(session_factory):
    org = await new_org(session_factory)
    ada, bob = make_membership(org.id, joined_at=NOW), make_membership(org.id, joined_at=NOW + timedelta(minutes=1))
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(ada)
        await MembershipRepository(s).add(bob)
        await MembershipRepository(s).add(make_membership(org.id, joined_at=NOW + timedelta(minutes=2)))  # unknown
        users = KnownUserRepository(s)
        await users.upsert(ada.user_id, "ada@amity.edu", "Ada Lovelace", NOW)
        await users.upsert(bob.user_id, "bob@amity.edu", "Bob", NOW)
    async with unit_of_work(session_factory) as s:
        repo = MembershipRepository(s)
        assert [u.full_name for _m, u in await repo.list_page(org.id, q="LOVELACE")] == ["Ada Lovelace"]
        assert [u.email for _m, u in await repo.list_page(org.id, q="bob@")] == ["bob@amity.edu"]
        everyone = await repo.list_page(org.id)
        assert len(everyone) == 3 and everyone[2][1] is None       # outer join keeps members with no known user


async def test_seat_state_round_trip(session_factory):
    org = await new_org(session_factory)
    async with unit_of_work(session_factory) as s:
        await SeatStateRepository(s).add(make_seat_state(org.id, plan_code="payg", seats_bought=None))
    async with unit_of_work(session_factory) as s:
        repo = SeatStateRepository(s)
        row = await repo.get(org.id)
        assert row.plan_code == "payg" and row.seats_bought is None
        row.seats_bought = 10
        await repo.save(row)
    async with unit_of_work(session_factory) as s:
        assert (await SeatStateRepository(s).lock_for_update(org.id)).seats_bought == 10


async def test_invites_and_batches(session_factory):
    org = await new_org(session_factory)
    batch = InviteBatch(id=uuid7(), org_id=org.id, status="queued", rows_total=2, created_at=NOW,
                        errors=[{"row": 3, "reason": "invalid_email"}])
    async with unit_of_work(session_factory) as s:
        repo = InviteRepository(s)
        await repo.add_batch(batch)
        a = await repo.add(make_invite(org.id, email="a@amity.edu", token_hash="h1", batch_id=batch.id))
        b = await repo.add(make_invite(org.id, email="b@amity.edu", token_hash="h2", created_at=NOW + timedelta(hours=1)))
        await repo.add(make_invite(org.id, kind="link", email=None, token_hash="h3", max_uses=5,
                                   created_at=NOW + timedelta(hours=2)))
    async with unit_of_work(session_factory) as s:
        repo = InviteRepository(s)
        assert (await repo.get(a.id)).email == "a@amity.edu"
        assert (await repo.get_by_token_hash("h2")).id == b.id
        assert await repo.get_by_token_hash("nope") is None
        assert (await repo.get_by_token_hash_for_update("h1")).id == a.id
        assert (await repo.get_open_for_email(org.id, "A@AMITY.EDU")).id == a.id
        pages = await repo.list_page(org.id, limit=2)
        assert [i.token_hash for i in pages] == ["h3", "h2"]
        assert [i.token_hash for i in await repo.list_page(org.id, after=(pages[-1].created_at, pages[-1].id))] == ["h1"]
        assert [i.token_hash for i in await repo.list_page(org.id, kind="link")] == ["h3"]
        assert await repo.count_created_since(org.id, NOW + timedelta(minutes=30)) == 1     # single invites only: the link is not counted
        got = await repo.get_batch(batch.id)
        assert got.errors == [{"row": 3, "reason": "invalid_email"}] and got.rows_total == 2
        got.status = "done"
        got.rows_ok = 2
        await repo.save_batch(got)
    async with unit_of_work(session_factory) as s:
        assert (await InviteRepository(s).get_batch(batch.id)).status == "done"


async def test_domains_and_join_requests(session_factory):
    org = await new_org(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        repo = DomainRepository(s)
        await repo.add(OrgDomain(org_id=org.id, domain="amity.edu", verify_token="tok"))
        await repo.add(OrgDomain(org_id=org.id, domain="cs.amity.edu", verify_token="tok2", verified_at=NOW,
                                 verify_method="dns", auto_join=True))
        req = await repo.add_join_request(JoinRequest(id=uuid7(), org_id=org.id, user_id=user, domain="amity.edu",
                                                      status="pending", created_at=NOW))
    async with unit_of_work(session_factory) as s:
        repo = DomainRepository(s)
        assert [d.domain for d in await repo.list_for_org(org.id)] == ["amity.edu", "cs.amity.edu"]
        assert (await repo.get(org.id, "amity.edu")).verify_token == "tok"
        assert await repo.get_verified("amity.edu") is None
        assert (await repo.get_verified("cs.amity.edu")).auto_join is True
        assert (await repo.get_join_request(req.id)).user_id == user
        assert len(await repo.list_join_requests(org.id, status="pending")) == 1
        assert await repo.list_join_requests(org.id, status="approved") == []
        await repo.delete(await repo.get(org.id, "amity.edu"))
    async with unit_of_work(session_factory) as s:
        assert [d.domain for d in await DomainRepository(s).list_for_org(org.id)] == ["cs.amity.edu"]


async def test_policy_round_trip_and_versioned_update(session_factory):
    org = await new_org(session_factory)
    modes = [uuid7(), uuid7()]
    async with unit_of_work(session_factory) as s:
        await PolicyRepository(s).add(OrgPolicy(org_id=org.id, lock_modes=True, approved_mode_ids=modes,
                                                member_spend_cap_paise=50_000, updated_at=NOW))
    async with unit_of_work(session_factory) as s:
        repo = PolicyRepository(s)
        p = await repo.get(org.id)
        assert (p.lock_modes, p.approved_mode_ids, p.member_spend_cap_paise, p.version) == (True, modes, 50_000, 1)
        assert await repo.update_if_version(org.id, 1, uuid7(), NOW, member_spend_cap_paise=None) is True
        assert await repo.update_if_version(org.id, 1, uuid7(), NOW, lock_modes=False) is False   # stale
        p = await repo.get(org.id)
        assert (p.version, p.member_spend_cap_paise, p.lock_modes) == (2, None, True)


async def test_audit_outbox_known_users_and_jobs(session_factory):
    org = await new_org(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        await AuditRepository(s).add(make_audit(org.id, action="org.created"))
        await OutboxRepository(s).add(make_outbox(next_attempt_at=NOW - timedelta(minutes=1)))
        await OutboxRepository(s).add(make_outbox(next_attempt_at=NOW + timedelta(hours=1)))
        await OutboxRepository(s).add(make_outbox(published_at=NOW))
        users = KnownUserRepository(s)
        await users.upsert(user, "Ada@Amity.edu", "Ada", NOW)
    async with unit_of_work(session_factory) as s:
        assert [a.action for a in await AuditRepository(s).list_page(org.id)] == ["org.created"]
        outbox = OutboxRepository(s)
        assert await outbox.count_unpublished() == 2
        due = await outbox.claim_due(NOW)
        assert len(due) == 1 and due[0].payload == {"x": 1}
        assert (await outbox.get(due[0].id)).topic == "org.member.joined"

        users = KnownUserRepository(s)
        await users.upsert(user, "ada@amity.edu", "Ada L", NOW + timedelta(minutes=1))
        await users.set_email_verified(user, "ada@amity.edu", NOW + timedelta(minutes=2))
        await users.upsert(user, "ada@amity.edu", "Ada Lovelace", NOW + timedelta(minutes=3))  # keeps verified_at
    async with unit_of_work(session_factory) as s:
        users = KnownUserRepository(s)
        ku = await users.get(user)
        assert (ku.full_name, ku.email_verified_at) == ("Ada Lovelace", NOW + timedelta(minutes=2))
        assert (await users.get_by_email("ADA@amity.EDU")).user_id == user       # citext
        assert list((await users.get_many([user, uuid7()])).keys()) == [user]
        assert await users.get_many([]) == {}
        await users.set_deleted(user, NOW + timedelta(days=1))
    async with unit_of_work(session_factory) as s:
        assert (await KnownUserRepository(s).get(user)).deleted_at == NOW + timedelta(days=1)


async def test_jobs_dedupe_and_claim(session_factory):
    async with unit_of_work(session_factory) as s:
        repo = JobRepository(s)
        assert await repo.add_unless_open(Job(id=uuid7(), kind="invites.expire", run_at=NOW, dedupe_key="invites.expire"))
        assert not await repo.add_unless_open(Job(id=uuid7(), kind="invites.expire", run_at=NOW,
                                                  dedupe_key="invites.expire"))      # already open: not queued twice
        plain = await repo.add(Job(id=uuid7(), kind="csv.send", run_at=NOW + timedelta(hours=1), payload={"b": 1}))
        await repo.add(Job(id=uuid7(), kind="csv.send", run_at=NOW + timedelta(hours=1)))  # no key: never deduped
    async with unit_of_work(session_factory) as s:
        repo = JobRepository(s)
        due = await repo.claim_due(NOW)
        assert [j.kind for j in due] == ["invites.expire"]
        due[0].done_at = NOW
        await s.flush()
        # once done, the same key may be queued again
        assert await repo.add_unless_open(Job(id=uuid7(), kind="invites.expire", run_at=NOW + timedelta(days=1),
                                              dedupe_key="invites.expire"))
        assert (await repo.get(plain.id)).payload == {"b": 1}
        assert len(await repo.claim_due(NOW + timedelta(days=2))) == 3


def test_every_repository_is_exported_and_takes_only_a_session():
    import inspect
    for repo in (AuditRepository, DomainRepository, InviteRepository, JobRepository, KnownUserRepository,
                 MembershipRepository, OrgRepository, OutboxRepository, PolicyRepository, SeatStateRepository):
        assert list(inspect.signature(repo.__init__).parameters) == ["self", "session"]
        assert not [n for n in dir(repo) if n in ("commit", "rollback")]


def test_models_expected_by_the_repositories_exist():
    assert Membership.__tablename__ == "memberships" and isinstance(uuid.uuid4(), uuid.UUID)
