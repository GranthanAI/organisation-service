"""Each known constraint maps to the right domain error; anything else is re-raised unchanged."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AlreadyInOrg, DomainTaken, InviteAlreadyOpen, JoinRequestPending
from app.db.errors import CONSTRAINT_ERRORS, constraint_name, map_integrity_error
from app.db.session import unit_of_work
from app.models.domain import JoinRequest, OrgDomain
from app.repositories.domain_repository import DomainRepository
from app.repositories.invite_repository import InviteRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.org_repository import OrgRepository
from app.utils.ids import uuid7
from tests.support.factories import NOW, make_invite, make_membership, make_org


async def two_orgs(session_factory):
    a, b = make_org(), make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(a)
        await OrgRepository(s).add(b)
    return a, b


def test_the_mapping_table():
    assert CONSTRAINT_ERRORS == {
        "ux_memberships_one_active_org": AlreadyInOrg,
        "ux_invites_open_email": InviteAlreadyOpen,
        "ux_org_domains_verified": DomainTaken,
        "ux_join_requests_pending": JoinRequestPending,
    }


async def test_active_membership_maps_to_already_in_org(session_factory):
    a, b = await two_orgs(session_factory)
    user = uuid7()
    async with unit_of_work(session_factory) as s:
        await MembershipRepository(s).add(make_membership(a.id, user))
    with pytest.raises(AlreadyInOrg) as exc:
        async with unit_of_work(session_factory) as s:
            await MembershipRepository(s).add(make_membership(b.id, user))
    assert exc.value.code == "ALREADY_IN_ORG"
    assert isinstance(exc.value.__cause__, IntegrityError)


async def test_open_invite_maps_to_invite_already_open(session_factory):
    a, _ = await two_orgs(session_factory)
    async with unit_of_work(session_factory) as s:
        await InviteRepository(s).add(make_invite(a.id, email="x@amity.edu"))
    with pytest.raises(InviteAlreadyOpen) as exc:
        async with unit_of_work(session_factory) as s:
            await InviteRepository(s).add(make_invite(a.id, email="X@Amity.EDU"))  # citext: same email
    assert exc.value.code == "INVITE_ALREADY_OPEN"


async def test_open_invite_rules_revoked_accepted_and_other_orgs_are_fine(session_factory):
    a, b = await two_orgs(session_factory)
    async with unit_of_work(session_factory) as s:
        repo = InviteRepository(s)
        await repo.add(make_invite(a.id, email="x@amity.edu", revoked_at=NOW, revoke_reason="revoked"))
        await repo.add(make_invite(a.id, email="x@amity.edu", accepted_at=NOW))
        await repo.add(make_invite(a.id, email="x@amity.edu"))          # the one open invite
        await repo.add(make_invite(b.id, email="x@amity.edu"))          # another org
        await repo.add(make_invite(a.id, kind="link", email=None))      # links are not per email
        await repo.add(make_invite(a.id, kind="link", email=None))


async def test_verified_domain_maps_to_domain_taken(session_factory):
    a, b = await two_orgs(session_factory)
    async with unit_of_work(session_factory) as s:
        await DomainRepository(s).add(OrgDomain(org_id=a.id, domain="amity.edu", verify_token="t1",
                                                verified_at=NOW, verify_method="dns"))
    with pytest.raises(DomainTaken) as exc:
        async with unit_of_work(session_factory) as s:
            await DomainRepository(s).add(OrgDomain(org_id=b.id, domain="amity.edu", verify_token="t2",
                                                    verified_at=NOW, verify_method="staff"))
    assert exc.value.code == "DOMAIN_TAKEN"


async def test_unverified_claims_do_not_collide_but_verifying_the_second_does(session_factory):
    a, b = await two_orgs(session_factory)
    async with unit_of_work(session_factory) as s:
        repo = DomainRepository(s)
        await repo.add(OrgDomain(org_id=a.id, domain="amity.edu", verify_token="t1",
                                 verified_at=NOW, verify_method="dns"))
        second = await repo.add(OrgDomain(org_id=b.id, domain="amity.edu", verify_token="t2"))
    with pytest.raises(DomainTaken):
        async with unit_of_work(session_factory) as s:
            repo = DomainRepository(s)
            row = await repo.get(b.id, "amity.edu")
            row.verified_at, row.verify_method = NOW, "staff"
            await repo.save(row)
    assert second.org_id == b.id


async def test_other_violations_are_reraised_unchanged(session_factory):
    a, _ = await two_orgs(session_factory)
    with pytest.raises(IntegrityError) as exc:
        async with unit_of_work(session_factory) as s:
            await OrgRepository(s).add(make_org(slug=a.slug))  # uq_organisations_slug is not mapped
    assert constraint_name(exc.value) == "uq_organisations_slug"
    assert map_integrity_error(exc.value) is None


async def test_check_constraint_violations_are_reraised_unchanged(session_factory):
    with pytest.raises(IntegrityError) as exc:
        async with unit_of_work(session_factory) as s:
            await OrgRepository(s).add(make_org(status="not-a-status"))
    assert constraint_name(exc.value) == "ck_organisations_status"
    assert map_integrity_error(exc.value) is None


async def test_pending_join_request_maps_to_join_request_pending(session_factory):
    a, b = await two_orgs(session_factory)
    user = uuid7()

    def request(org, status="pending"):
        return JoinRequest(id=uuid7(), org_id=org.id, user_id=user, domain="amity.edu", status=status, created_at=NOW)

    async with unit_of_work(session_factory) as s:
        await DomainRepository(s).add_join_request(request(a))
    with pytest.raises(JoinRequestPending) as exc:
        async with unit_of_work(session_factory) as s:
            await DomainRepository(s).add_join_request(request(a))
    assert exc.value.code == "JOIN_REQUEST_PENDING"
    # another org, or a decided request, is fine
    async with unit_of_work(session_factory) as s:
        await DomainRepository(s).add_join_request(request(b))
        await DomainRepository(s).add_join_request(request(a, status="declined"))
        await DomainRepository(s).add_join_request(request(a, status="approved"))


# --- the CHECK constraints added by 0005 (each is re-raised unchanged as IntegrityError) ---
from app.models.idempotency_key import IdempotencyKey  # noqa: E402
from app.repositories.seat_state_repository import SeatStateRepository  # noqa: E402
from tests.support.factories import make_seat_state  # noqa: E402


async def test_ck_idempotency_keys_state(session_factory):
    def row(state, **kw):
        return IdempotencyKey(actor_id=uuid7(), route="POST /x", key=uuid7().hex, request_hash="h", state=state,
                              created_at=NOW, **kw)

    async with unit_of_work(session_factory) as s:  # a reservation has no status or body yet
        s.add(row("in_progress", locked_until=NOW))
        s.add(row("done", status_code=201, response={"ok": True}))
    with pytest.raises(IntegrityError) as exc:
        async with unit_of_work(session_factory) as s:
            s.add(row("started"))
    assert constraint_name(exc.value) == "ck_idempotency_keys_state"


@pytest.mark.parametrize("status", ["incomplete", "active", "past_due", "suspended", "cancelling", "ended"])
async def test_ck_seat_state_status_accepts_billing_statuses(session_factory, status):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await SeatStateRepository(s).add(make_seat_state(org.id, status=status))


async def test_ck_seat_state_status_rejects_anything_else(session_factory):
    org = make_org()
    with pytest.raises(IntegrityError) as exc:
        async with unit_of_work(session_factory) as s:
            await OrgRepository(s).add(org)
            await SeatStateRepository(s).add(make_seat_state(org.id, status="paused"))
    assert constraint_name(exc.value) == "ck_seat_state_status"


@pytest.mark.parametrize("reason", ["revoked", "expired", "resent", "user_deleted"])
async def test_ck_invites_revoke_reason_accepts_the_four_reasons(session_factory, reason):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        await InviteRepository(s).add(make_invite(org.id, revoked_at=NOW, revoke_reason=reason))


async def test_ck_invites_revoke_reason_rejects_unknown(session_factory):
    org = make_org()
    with pytest.raises(IntegrityError) as exc:
        async with unit_of_work(session_factory) as s:
            await OrgRepository(s).add(org)
            await InviteRepository(s).add(make_invite(org.id, revoked_at=NOW, revoke_reason="because"))
    assert constraint_name(exc.value) == "ck_invites_revoke_reason"


@pytest.mark.parametrize("fields", [{"revoked_at": NOW}, {"revoke_reason": "revoked"}])
async def test_ck_invites_revoked_together(session_factory, fields):
    org = make_org()
    with pytest.raises(IntegrityError) as exc:
        async with unit_of_work(session_factory) as s:
            await OrgRepository(s).add(org)
            await InviteRepository(s).add(make_invite(org.id, **fields))
    assert constraint_name(exc.value) == "ck_invites_revoked_together"


async def test_an_expired_invite_closed_with_reason_expired_frees_the_open_email_slot(session_factory):
    org = make_org()
    async with unit_of_work(session_factory) as s:
        await OrgRepository(s).add(org)
        old = await InviteRepository(s).add(make_invite(org.id, email="late@amity.edu"))
    with pytest.raises(InviteAlreadyOpen):
        async with unit_of_work(session_factory) as s:
            await InviteRepository(s).add(make_invite(org.id, email="late@amity.edu"))
    async with unit_of_work(session_factory) as s:  # the Phase 7 rule: past expires_at, close it as revoked/expired
        repo = InviteRepository(s)
        row = await repo.get(old.id)
        row.revoked_at, row.revoke_reason = NOW, "expired"
        await repo.save(row)
        await repo.add(make_invite(org.id, email="late@amity.edu"))
