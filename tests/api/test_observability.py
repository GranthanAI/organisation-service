"""P10d: every 4xx and 5xx is logged once (code, route template, status, correlation id; never bodies, tokens or emails), and the
business counters move."""

import uuid

import pytest
from structlog.testing import capture_logs

from app.core.config import settings
from app.services import invite_service
from app.telemetry.metrics import DEPENDENCY_UP, INVITES_CREATED, JOINS, SEAT_FULL
from tests.support.factories import ensure_known
from tests.support.tokens import bearer, make_token


def failures(logs):
    return [e for e in logs if e.get("event") == "request failed"]


async def test_a_404_is_logged_once_with_the_route_template_and_the_correlation_id(api):
    world = await api.seed_org()
    with capture_logs() as logs:
        r = await api.client.get(f"/org/v1/orgs/{world.org.id}/invites/{uuid.uuid4()}/nope", headers={**api.h(world.owner), "X-Correlation-ID": "trace-1"})
    (entry,) = failures(logs)
    assert r.status_code == 404 and (entry["code"], entry["status"], entry["correlation_id"], entry["method"]) == ("NOT_FOUND", 404, "trace-1", "GET")


@pytest.mark.parametrize("make_request, status, code", [
    (lambda api, w: api.client.get(f"/org/v1/orgs/{w.org.id}/members", params={"limit": 0}, headers=api.h(w.owner)), 422, "VALIDATION_ERROR"),
    (lambda api, w: api.client.get(f"/org/v1/orgs/{w.org.id}/members", headers=api.h(w.members[0])), 403, "FORBIDDEN_ROLE"),
    (lambda api, w: api.client.get(f"/org/v1/orgs/{uuid.uuid4()}/members", headers=api.h(w.owner)), 404, "NOT_FOUND"),
    (lambda api, w: api.client.get(f"/org/v1/orgs/{w.org.id}/members"), 401, "UNAUTHENTICATED"),
])
async def test_each_error_family_is_logged_exactly_once(api, make_request, status, code):
    world = await api.seed_org()
    with capture_logs() as logs:
        r = await make_request(api, world)
    (entry,) = failures(logs)
    assert r.status_code == status and entry["code"] == code and entry["status"] == status
    assert "{org_id}" in entry["route"] or entry["route"].startswith("/org/v1") and str(world.org.id) not in entry["route"]
    assert entry["correlation_id"] and entry["correlation_id"] == r.headers["x-correlation-id"]


async def test_a_5xx_is_logged_at_error_level_once(api, monkeypatch):
    world = await api.seed_org()

    async def boom(*args, **kwargs):
        raise RuntimeError("something broke")

    monkeypatch.setattr(invite_service, "create_invites", boom)
    with capture_logs() as logs:
        r = await api.post(f"/org/v1/orgs/{world.org.id}/invites", {"emails": ["a@amity.edu"]}, api.h(world.owner))
    (entry,) = failures(logs)
    assert r.status_code == 500 and (entry["code"], entry["status"], entry["log_level"]) == ("INTERNAL_ERROR", 500, "error")


async def test_a_413_is_logged_once(api, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_BYTES", 100)
    with capture_logs() as logs:
        r = await api.client.post("/org/v1/join-requests", content=b"x" * 500, headers=bearer(make_token()))
    (entry,) = failures(logs)
    assert r.status_code == 413 and (entry["code"], entry["status"]) == ("REQUEST_TOO_LARGE", 413)


async def test_the_log_never_carries_a_token_an_email_or_a_body(api):
    world = await api.seed_org()
    token = make_token(sub=world.owner.id, email="private.person@amity.edu")
    with capture_logs() as logs:
        await api.client.post(f"/org/v1/orgs/{world.org.id}/invites", json={"emails": ["secret.student@amity.edu"], "role": "wizard"},
                              headers={**bearer(token), "Idempotency-Key": uuid.uuid4().hex})
    text = repr(logs)
    assert failures(logs) and token not in text and "private.person@amity.edu" not in text and "secret.student@amity.edu" not in text
    assert "wizard" not in text


async def test_a_success_logs_no_failure_line(api):
    world = await api.seed_org()
    with capture_logs() as logs:
        assert (await api.client.get("/org/v1/me", headers=api.h(world.owner))).status_code == 200
    assert failures(logs) == []


# ---------------------------------------------------------------- counters
def value(counter, **labels) -> float:
    return (counter.labels(**labels) if labels else counter)._value.get()


async def test_joins_and_seat_full_are_counted(api):
    world = await api.seed_org(seats=5, admins=1, members=3)                                  # 5 of 5
    ok_before = value(JOINS, source="invite", result="ok")
    full_before, seat_full_before = value(JOINS, source="invite", result="seats_full"), value(SEAT_FULL)
    user = await ensure_known(api.sf, uuid.uuid4(), "late@amity.edu")
    from tests.support.invites import seed_invite
    _, token = await seed_invite(api.sf, world.org.id, email="late@amity.edu")
    r = await api.post("/org/v1/invites/accept", {"token": token}, bearer(make_token(sub=user, email="late@amity.edu")))
    assert r.status_code == 409
    assert value(JOINS, source="invite", result="seats_full") == full_before + 1 and value(SEAT_FULL) == seat_full_before + 1
    assert value(JOINS, source="invite", result="ok") == ok_before
    from app.db.session import unit_of_work
    from app.models.seat_state import SeatState
    async with unit_of_work(api.sf) as s:                                                             # a seat appears
        (await s.get(SeatState, world.org.id)).seats_bought = 6
    assert (await api.post("/org/v1/invites/accept", {"token": token}, bearer(make_token(sub=user, email="late@amity.edu")))).status_code == 200
    assert value(JOINS, source="invite", result="ok") == ok_before + 1


async def test_invites_created_are_counted_by_kind(api):
    world = await api.seed_org()
    email, csv_, link = (value(INVITES_CREATED, kind=k) for k in ("email", "csv", "link"))
    await api.post(f"/org/v1/orgs/{world.org.id}/invites", {"emails": ["a@amity.edu", "b@amity.edu"]}, api.h(world.owner))
    await api.client.post(f"/org/v1/orgs/{world.org.id}/invites/csv", files={"file": ("a.csv", b"email\nc@amity.edu\nd@amity.edu\ne@amity.edu\n", "text/csv")},
                          headers={**api.h(world.owner), "Idempotency-Key": uuid.uuid4().hex})
    await api.post(f"/org/v1/orgs/{world.org.id}/invite-links", {"max_uses": 5, "expires_in_days": 7}, api.h(world.owner))
    assert value(INVITES_CREATED, kind="email") == email + 2
    assert value(INVITES_CREATED, kind="csv") == csv_ + 3
    assert value(INVITES_CREATED, kind="link") == link + 1


async def test_ready_sets_the_dependency_gauge(api):
    from app.api.deps import get_probes
    from app.main import app

    async def up():
        return True

    async def down():
        return False

    app.dependency_overrides[get_probes] = lambda: {"postgres": up, "redis": down}
    r = await api.client.get("/ready")
    assert r.status_code == 200 and value(DEPENDENCY_UP, dependency="postgres") == 1 and value(DEPENDENCY_UP, dependency="redis") == 0
    app.dependency_overrides[get_probes] = lambda: {"postgres": down}
    assert (await api.client.get("/ready")).status_code == 503 and value(DEPENDENCY_UP, dependency="postgres") == 0
