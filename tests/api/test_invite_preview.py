"""GET /org/v1/invites/preview?token=: public (no JWT), rate limited per IP, and says nothing it should not."""

import uuid
from datetime import timedelta

import pytest

from app.core.constants import RATE_INVITE_PREVIEW
from app.services.invite_service import email_hint
from tests.support.factories import NOW
from tests.support.invites import seed_invite

PREVIEW = "/org/v1/invites/preview"


async def preview(api, token, **headers):
    return await api.client.get(PREVIEW, params={"token": token}, headers=headers)


async def test_a_valid_invite_shows_the_join_page_data_without_a_jwt(api):
    world = await api.seed_org(name="Amity University", type="college")
    invite, token = await seed_invite(api.sf, world.org.id, email="student@amity.edu", role="admin",
                                      created_by=world.owner.id)
    r = await preview(api, token)                                              # no Authorization header at all
    assert r.status_code == 200
    assert r.json() == {"org_name": "Amity University", "org_type": "college", "inviter_name": world.owner.name,
                        "role": "admin", "email_hint": "s***@amity.edu", "expires_at": r.json()["expires_at"]}
    assert r.json()["expires_at"].startswith((NOW + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M"))
    for secret in (invite.token_hash, token, "student@amity.edu", str(world.org.id), str(invite.id)):
        assert secret not in r.text


async def test_the_inviter_name_falls_back_when_unknown(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, created_by=None)
    assert (await preview(api, token)).json()["inviter_name"] == "An organisation admin"
    _, token2 = await seed_invite(api.sf, world.org.id, email="b@amity.edu", created_by=uuid.uuid4())     # not a known user
    assert (await preview(api, token2)).json()["inviter_name"] == "An organisation admin"


@pytest.mark.parametrize("email, hint", [("student@amity.edu", "s***@amity.edu"), ("a@x.co", "a***@x.co"),
                                         ("Dean.Of.Studies@Amity.EDU", "d***@amity.edu")])
async def test_the_email_hint_masks_the_local_part(api, email, hint):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id, email=email.lower())
    assert (await preview(api, token)).json()["email_hint"] == hint


def test_email_hint_edge_cases():
    assert email_hint(None) == "" and email_hint("no-at-sign") == "" and email_hint("x@y.zz") == "x***@y.zz"


async def test_an_expired_invite_is_410_whether_or_not_the_job_has_closed_it(api):
    world = await api.seed_org()
    _, open_but_late = await seed_invite(api.sf, world.org.id, email="a@amity.edu", expires_in=-timedelta(seconds=1))
    r = await preview(api, open_but_late)
    assert r.status_code == 410 and r.json()["code"] == "INVITE_EXPIRED"
    _, closed_by_job = await seed_invite(api.sf, world.org.id, email="b@amity.edu", expires_in=-timedelta(days=3),
                                         revoked_at=NOW, revoke_reason="expired")
    assert (await preview(api, closed_by_job)).status_code == 410


@pytest.mark.parametrize("state", ["revoked", "accepted", "purged", "unknown", "garbage"])
async def test_revoked_accepted_purged_and_unknown_are_404(api, state):
    world = await api.seed_org()
    kw = {"revoked": dict(revoked_at=NOW, revoke_reason="revoked"), "accepted": dict(accepted_at=NOW, accepted_by=uuid.uuid4()),
          "purged": dict(purged_at=NOW)}.get(state, {})
    email = None if state == "purged" else "x@amity.edu"
    _, token = await seed_invite(api.sf, world.org.id, email=email, **kw)
    if state == "unknown":
        token = "A" * 43
    elif state == "garbage":
        token = "not-a-real-token-at-all"
    r = await preview(api, token)
    assert r.status_code == 404 and r.json()["code"] == "INVITE_INVALID"


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_an_org_that_cannot_take_members_is_404_not_a_leak(api, status):
    world = await api.seed_org(status=status)
    _, token = await seed_invite(api.sf, world.org.id)
    r = await preview(api, token)
    assert r.status_code == 404 and r.json()["code"] == "INVITE_INVALID" and status not in r.text


@pytest.mark.parametrize("status", ["pilot", "active"])
async def test_pilot_and_active_orgs_preview_fine(api, status):
    world = await api.seed_org(status=status)
    _, token = await seed_invite(api.sf, world.org.id)
    assert (await preview(api, token)).status_code == 200


async def test_a_resent_invite_old_link_is_404_and_the_new_one_works(api):
    world = await api.seed_org()
    invite, old = await seed_invite(api.sf, world.org.id, created_by=world.owner.id)
    assert (await preview(api, old)).status_code == 200
    r = await api.post(f"/org/v1/orgs/{world.org.id}/invites/{invite.id}/resend", headers=api.h(world.owner))
    assert r.status_code == 200
    assert (await preview(api, old)).status_code == 404
    from app.security.tokens import invite_token
    assert (await preview(api, invite_token(invite.id, 2))).status_code == 200


async def test_a_missing_or_tiny_token_is_a_validation_error(api):
    assert (await api.client.get(PREVIEW)).status_code == 422
    assert (await api.client.get(PREVIEW, params={"token": "abc"})).status_code == 422


async def test_it_needs_no_idempotency_key_and_is_the_only_public_invite_route(api):
    world = await api.seed_org()
    invite, token = await seed_invite(api.sf, world.org.id)
    assert (await preview(api, token)).status_code == 200
    assert (await api.client.get(f"/org/v1/orgs/{world.org.id}/invites")).status_code == 401
    assert (await api.client.post("/org/v1/invites/accept", json={"token": token},
                                  headers={"Idempotency-Key": uuid.uuid4().hex})).status_code == 401


async def test_the_rate_limit_is_per_ip(api):
    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id)
    limit = RATE_INVITE_PREVIEW.limit
    codes = [(await preview(api, token, **{"X-Forwarded-For": "203.0.113.7"})).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [200] * limit and codes[-1] == 429
    blocked = await preview(api, token, **{"X-Forwarded-For": "203.0.113.7"})
    assert blocked.json()["code"] == "RATE_LIMITED" and int(blocked.headers["retry-after"]) > 0
    assert (await preview(api, token, **{"X-Forwarded-For": "198.51.100.9"})).status_code == 200      # another IP is unaffected


async def test_it_still_works_when_redis_is_down_because_the_limit_fails_open(api):
    from app.api.deps import get_redis_client
    from app.main import app

    class Broken:
        def pipeline(self, *a, **k):
            raise ConnectionError("redis is down")

    world = await api.seed_org()
    _, token = await seed_invite(api.sf, world.org.id)
    app.dependency_overrides[get_redis_client] = lambda: Broken()
    assert (await preview(api, token)).status_code == 200
