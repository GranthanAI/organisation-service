"""Each known constraint maps to the right domain error; anything else is re-raised unchanged."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AlreadyInOrg, DomainTaken, InviteAlreadyOpen
from app.db.errors import CONSTRAINT_ERRORS, constraint_name, map_integrity_error
from app.db.session import unit_of_work
from app.models.domain import OrgDomain
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
        await repo.add(make_invite(a.id, email="x@amity.edu", revoked_at=NOW))
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
