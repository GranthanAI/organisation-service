"""POST /org/v1/orgs/{org_id}/invites/csv: validated and created in one request; only the emails go out later."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.invite import Invite, InviteBatch
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.security.tokens import invite_token_hash
from tests.support.factories import NOW, make_invite


def url(world) -> str:
    return f"/org/v1/orgs/{world.org.id}/invites/csv"


async def upload(api, world, who, text, *, role=None, dry_run=None, key=None, filename="students.csv", raw=None):
    data = {k: v for k, v in (("role", role), ("dry_run", None if dry_run is None else str(dry_run).lower())) if v is not None}
    body = raw if raw is not None else text.encode("utf-8")
    return await api.client.post(url(world), files={"file": (filename, body, "text/csv")}, data=data,
                                 headers={**api.h(who), "Idempotency-Key": key or uuid.uuid4().hex})


async def table(api, model):
    async with api.sf() as s:
        return list((await s.execute(select(model))).scalars())


async def count(api, model, *where):
    async with api.sf() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


def sheet(*emails, header="email") -> str:
    return header + "\n" + "".join(f"{e}\n" for e in emails)


async def test_every_reason_with_the_right_row_number(api):
    world, other = await api.seed_org(), await api.seed_org()
    await api.client.get("/org/v1/me", headers=api.h(world.owner))
    from app.db.session import unit_of_work
    async with unit_of_work(api.sf) as s:                                   # an open invite and a long-expired open invite
        s.add(make_invite(world.org.id, email="open@amity.edu", token_hash="h1"))
        s.add(make_invite(world.org.id, email="late@amity.edu", token_hash="h2", expires_at=NOW - timedelta(days=1)))
    text = "\n".join([
        "Email,Name,Role",
        "good1@amity.edu,Good One,",                       # row 2  ok
        "not-an-email,Bad,",                               # row 3  invalid_email
        "GOOD1@amity.edu,Dup,",                            # row 4  duplicate_in_file (case-insensitive)
        "good2@amity.edu,Two,owner",                       # row 5  role_not_allowed
        f"{world.members[0].email},Already,",              # row 6  already_member
        f"{other.members[0].email},Elsewhere,",            # row 7  in_another_org
        "open@amity.edu,Open,",                            # row 8  open_invite_exists
        "late@amity.edu,Late,",                            # row 9  ok: its expired invite is closed first (H4)
        "",                                                # row 10 blank
        ",Nobody,",                                        # row 11 invalid_email (empty)
        "good3@amity.edu,Three,Member",                    # row 12 ok
    ]) + "\n"
    r = await upload(api, world, world.admin, text)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["rows_total"], body["rows_ok"], body["rows_failed"], body["dry_run"]) == (10, 3, 7, False)
    assert [(e["row"], e["reason"]) for e in body["errors_preview"]] == [
        (3, "invalid_email"), (4, "duplicate_in_file"), (5, "role_not_allowed"), (6, "already_member"),
        (7, "in_another_org"), (8, "open_invite_exists"), (11, "invalid_email")]
    assert body["errors_preview"][5]["email"] == "open@amity.edu" and body["errors_preview"][0]["email"] == "not-an-email"
    assert other.org.name not in r.text and str(other.org.id) not in r.text           # in_another_org never names the org
    created = {str(i.email): i for i in await table(api, Invite) if i.kind == "csv"}
    assert set(created) == {"good1@amity.edu", "late@amity.edu", "good3@amity.edu"}
    assert created["good1@amity.edu"].invitee_name == "Good One" and created["good3@amity.edu"].role == "member"
    late_old = [i for i in await table(api, Invite) if i.token_hash == "h2"][0]
    assert late_old.revoke_reason == "expired" and late_old.revoked_at == NOW


async def test_a_real_upload_writes_invites_events_jobs_and_one_audit_row(api):
    world = await api.seed_org()
    r = await upload(api, world, world.admin, "email,name\na@amity.edu,Ann\nb@amity.edu,Bob\nbad\n")
    body = r.json()
    assert (body["rows_ok"], body["rows_failed"]) == (2, 1)
    batch_id = uuid.UUID(body["batch_id"])
    invites = [i for i in await table(api, Invite) if i.kind == "csv"]
    assert len(invites) == 2 and all(i.batch_id == batch_id and i.token_version == 1 and i.uses == 0 for i in invites)
    assert all(i.token_hash == invite_token_hash(i.id, 1) and i.created_by == world.admin.id for i in invites)
    assert all(i.expires_at == NOW + timedelta(days=settings.INVITE_TTL_DAYS) for i in invites)
    events = [e for e in await table(api, OutboxEvent) if e.topic == "org.invite.created"]
    assert {e.payload["email"] for e in events} == {"a@amity.edu", "b@amity.edu"}
    assert all(set(e.payload) >= {"org_id", "invite_id", "email", "role", "org_name", "inviter_name", "expires_at"}
               and "token" not in str(e.payload) and "/join/" not in str(e.payload) for e in events)
    jobs = [j for j in await table(api, Job) if j.kind == "invite.send"]
    assert {j.payload["invite_id"] for j in jobs} == {str(i.id) for i in invites} and all(j.payload["token_version"] == 1 for j in jobs)
    audits = [a for a in await table(api, AuditLog) if a.action.startswith("invite.")]
    assert len(audits) == 1 and audits[0].action == "invite.csv_uploaded"
    assert audits[0].details == {"batch_id": str(batch_id), "rows_total": 3, "rows_ok": 2, "rows_failed": 1}
    (batch,) = await table(api, InviteBatch)
    assert (batch.status, batch.dry_run, batch.rows_total, batch.rows_ok, batch.rows_failed) == ("sending", False, 3, 2, 1)
    assert batch.errors == [{"row": 4, "email": "bad", "reason": "invalid_email"}]
    assert body["seats"] == {"used": 5, "bought": 50, "open_invites": 2, "payment_due": False}
    await api.run_jobs()
    assert sorted(m.to for m in api.mail.sent) == ["a@amity.edu", "b@amity.edu"]


async def test_dry_run_writes_only_the_batch_row(api):
    world = await api.seed_org()
    r = await upload(api, world, world.admin, "email\na@amity.edu\nb@amity.edu\nnope\n", dry_run=True)
    body = r.json()
    assert (body["dry_run"], body["rows_total"], body["rows_ok"], body["rows_failed"]) == (True, 3, 2, 1)
    assert await count(api, Invite) == 0 and await count(api, Job) == 0
    assert await count(api, OutboxEvent, OutboxEvent.topic == "org.invite.created") == 0
    assert await count(api, AuditLog, AuditLog.action.like("invite.%")) == 0
    (batch,) = await table(api, InviteBatch)
    assert batch.dry_run is True and batch.status == "done" and batch.finished_at == NOW
    assert batch.errors == [{"row": 4, "email": "nope", "reason": "invalid_email"}]
    again = await upload(api, world, world.admin, "email\na@amity.edu\n")                   # nothing was reserved by the dry run
    assert again.json()["rows_ok"] == 1


@pytest.mark.parametrize("kind, kw, rows, ok", [
    ("paid", dict(seats=10, admins=1, members=3), 8, 5),                     # used 5, free 5
    ("pilot", dict(status="pilot", plan_code="pilot", seats=25, admins=1, members=20), 6, 3),   # PILOT_MAX 25, used 22
    ("payg", dict(seats=None, seat_status="active"), 20, 20),                # no cap
    ("incomplete", dict(seats=None, seat_status="incomplete"), 4, 0),        # no seats at all
    ("ended", dict(seats=None, seat_status="ended"), 4, 0),
])
async def test_over_seats(api, kind, kw, rows, ok):
    world = await api.seed_org(**kw)
    r = await upload(api, world, world.owner, sheet(*[f"s{i}@amity.edu" for i in range(rows)]))
    body = r.json()
    assert (body["rows_ok"], body["rows_failed"]) == (ok, rows - ok)
    assert [e["row"] for e in body["errors_preview"]] == list(range(2 + ok, 2 + rows))          # the LAST rows, in file order
    assert {e["reason"] for e in body["errors_preview"]} <= {"over_seats"}
    assert await count(api, Invite, Invite.kind == "csv") == ok


async def test_open_invites_use_up_free_seats_and_a_link_does_not(api):
    world = await api.seed_org(seats=8, admins=1, members=3)                  # used 5, free 3
    from app.db.session import unit_of_work
    async with unit_of_work(api.sf) as s:
        s.add(make_invite(world.org.id, email="open1@amity.edu", token_hash="o1"))
        s.add(make_invite(world.org.id, kind="link", email=None, token_hash="l1", max_uses=100))
    r = await upload(api, world, world.owner, sheet("a@amity.edu", "b@amity.edu", "c@amity.edu"))
    assert (r.json()["rows_ok"], r.json()["rows_failed"]) == (2, 1)           # free = 8 - 5 - 1 open email invite


async def test_pilot_cap_applies_even_when_more_seats_are_bought(api):
    world = await api.seed_org(status="pilot", plan_code="pilot", seats=100, admins=0, members=22)   # used 23, cap 25
    r = await upload(api, world, world.owner, sheet("a@amity.edu", "b@amity.edu", "c@amity.edu"))
    assert (r.json()["rows_ok"], r.json()["rows_failed"]) == (2, 1)


async def test_role_admin_needs_the_owner(api):
    world = await api.seed_org()
    text = "email,role\na@amity.edu,admin\nb@amity.edu,member\nc@amity.edu,\n"
    by_admin = (await upload(api, world, world.admin, text)).json()
    assert [(e["row"], e["reason"]) for e in by_admin["errors_preview"]] == [(2, "role_not_allowed")] and by_admin["rows_ok"] == 2
    by_owner = (await upload(api, world, world.owner, "email,role\nd@amity.edu,admin\n")).json()
    assert by_owner["rows_ok"] == 1
    roles = {str(i.email): i.role for i in await table(api, Invite) if i.kind == "csv"}
    assert roles == {"b@amity.edu": "member", "c@amity.edu": "member", "d@amity.edu": "admin"}


async def test_role_admin_for_the_whole_upload(api):
    world = await api.seed_org()
    refused = await upload(api, world, world.admin, sheet("a@amity.edu"), role="admin")
    assert refused.status_code == 403 and refused.json()["code"] == "FORBIDDEN_ROLE"
    assert await count(api, Invite) == 0
    ok = await upload(api, world, world.owner, "email,role\na@amity.edu,\nb@amity.edu,member\n", role="admin")
    assert ok.status_code == 200
    roles = {str(i.email): i.role for i in await table(api, Invite)}
    assert roles == {"a@amity.edu": "admin", "b@amity.edu": "member"}               # a row's own role wins; blank uses the form role
    assert (await upload(api, world, world.owner, sheet("z@amity.edu"), role="owner")).status_code == 422


async def test_the_daily_row_limit_refuses_a_whole_upload(api, monkeypatch):
    monkeypatch.setattr(settings, "CSV_ROWS_PER_ORG_PER_DAY", 5)
    world = await api.seed_org()
    assert (await upload(api, world, world.owner, sheet("a1@amity.edu", "a2@amity.edu", "a3@amity.edu"))).status_code == 200
    r = await upload(api, world, world.owner, sheet("b1@amity.edu", "b2@amity.edu", "b3@amity.edu"))
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and int(r.headers["retry-after"]) > 0
    assert await count(api, Invite) == 3                                                # nothing of the refused upload
    dry = await upload(api, world, world.owner, sheet("b1@amity.edu", "b2@amity.edu", "b3@amity.edu"), dry_run=True)
    assert dry.status_code == 429                                                       # a dry run predicts the real answer
    assert (await upload(api, world, world.owner, sheet("c1@amity.edu", "c2@amity.edu"))).status_code == 200   # exactly 5
    other = await api.seed_org()
    assert (await upload(api, other, other.owner, sheet("x1@amity.edu"))).status_code == 200        # per organisation
    api.clock.advance(seconds=86400)                                                    # the next UTC day
    assert (await upload(api, world, world.owner, sheet("d1@amity.edu"))).status_code == 200


async def test_the_csv_limit_and_the_single_invite_limit_are_separate(api, monkeypatch):
    monkeypatch.setattr(settings, "INVITES_PER_ORG_PER_DAY", 2)
    world = await api.seed_org()
    assert (await upload(api, world, world.owner, sheet(*[f"s{i}@amity.edu" for i in range(6)]))).status_code == 200
    single = await api.post(f"/org/v1/orgs/{world.org.id}/invites", {"emails": ["one@amity.edu", "two@amity.edu"]}, api.h(world.owner))
    assert single.status_code == 200 and [r["result"] for r in single.json()["results"]] == ["created", "created"]
    again = await api.post(f"/org/v1/orgs/{world.org.id}/invites", {"emails": ["three@amity.edu"]}, api.h(world.owner))
    assert again.status_code == 429                                                     # only single invites counted


@pytest.mark.parametrize("status", ["suspended", "pilot_expired", "closed"])
async def test_the_status_gate(api, status):
    world = await api.seed_org(status=status)
    r = await upload(api, world, world.owner, sheet("a@amity.edu"))
    assert r.status_code == 409 and r.json()["code"] == "ORG_INACTIVE"
    assert await count(api, InviteBatch) == 0


async def test_who_may_upload(api):
    world, other = await api.seed_org(), await api.seed_org()
    assert (await upload(api, world, world.members[0], sheet("a@amity.edu"))).status_code == 403
    assert (await upload(api, world, other.owner, sheet("a@amity.edu"))).status_code == 404      # not a member: 404
    unauth = await api.client.post(url(world), files={"file": ("a.csv", b"email\n", "text/csv")},
                                   headers={"Idempotency-Key": uuid.uuid4().hex})
    assert unauth.status_code == 401
    assert await count(api, Invite) == 0


async def test_file_errors_are_csv_invalid(api):
    world = await api.seed_org()
    cases = [
        (dict(raw="email,name\na@amity.edu,José\n".encode("cp1252")), "save the file as CSV UTF-8"),
        (dict(text="name\nAnn\n"), "email column"),
        (dict(text=""), "empty"),
        (dict(raw=b"x" * (settings.CSV_MAX_BYTES + 5)), str(settings.CSV_MAX_BYTES)),
        (dict(text=sheet(*[f"s{i}@amity.edu" for i in range(settings.CSV_MAX_ROWS + 1)])), str(settings.CSV_MAX_ROWS)),
    ]
    for kw, detail in cases:
        r = await upload(api, world, world.owner, kw.pop("text", ""), **kw)
        assert r.status_code == 422 and r.json()["code"] == "CSV_INVALID" and detail in r.json()["detail"], (detail, r.text[:200])
    assert await count(api, InviteBatch) == 0


async def test_missing_file_and_bad_form_values(api):
    world = await api.seed_org()
    r = await api.client.post(url(world), data={"role": "member"}, headers={**api.h(world.owner), "Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 422
    assert (await upload(api, world, world.owner, sheet("a@amity.edu"), role="wizard")).status_code == 422
    assert (await upload(api, world, world.owner, sheet("a@amity.edu"), role="Member")).status_code == 200   # role is case-insensitive


async def test_the_upload_needs_an_idempotency_key_and_replays_with_it(api):
    world = await api.seed_org()
    no_key = await api.client.post(url(world), files={"file": ("a.csv", b"email\na@amity.edu\n", "text/csv")}, headers=api.h(world.owner))
    assert no_key.status_code == 422
    first = await upload(api, world, world.owner, sheet("a@amity.edu"), key="csv-upload-key-1")
    second = await upload(api, world, world.owner, sheet("a@amity.edu"), key="csv-upload-key-1")      # a new multipart boundary
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers.get("idempotent-replayed") == "true" and second.json() == first.json()
    assert await count(api, InviteBatch) == 1 and await count(api, Invite) == 1
    different = await upload(api, world, world.owner, sheet("zzz@amity.edu"), key="csv-upload-key-1")
    assert different.status_code == 422 and different.json()["code"] == "IDEMPOTENCY_MISMATCH"


async def test_two_uploads_with_the_same_email_at_once_give_one_invite(api):
    world = await api.seed_org()
    results = await asyncio.gather(
        upload(api, world, world.owner, sheet("same@amity.edu", "u1@amity.edu")),
        upload(api, world, world.admin, sheet("same@amity.edu", "u2@amity.edu")))
    assert [r.status_code for r in results] == [200, 200]
    invites = [i for i in await table(api, Invite) if str(i.email) == "same@amity.edu"]
    assert len(invites) == 1
    losers = [e for r in results for e in r.json()["errors_preview"] if e["email"] == "same@amity.edu"]
    assert [e["reason"] for e in losers] == ["open_invite_exists"]
    assert sum(r.json()["rows_ok"] for r in results) == 3                                  # 2 + 2 - the lost row
    events = [e for e in await table(api, OutboxEvent) if e.topic == "org.invite.created" and e.payload["email"] == "same@amity.edu"]
    assert len(events) == 1
    sends = [j for j in await table(api, Job) if j.kind == "invite.send"]
    assert len(sends) == 3
    assert {b.rows_ok for b in await table(api, InviteBatch)} <= {1, 2}


async def test_notification_mode_creates_invites_and_events_but_no_send_jobs(api, monkeypatch):
    monkeypatch.setattr(settings, "INVITE_EMAILS_VIA", "notification")
    world = await api.seed_org()
    r = await upload(api, world, world.owner, sheet("a@amity.edu", "b@amity.edu"))
    assert r.json()["rows_ok"] == 2 and await count(api, Job, Job.kind == "invite.send") == 0
    assert await count(api, OutboxEvent, OutboxEvent.topic == "org.invite.created") == 2
    (batch,) = await table(api, InviteBatch)
    assert batch.status == "done" and batch.finished_at == NOW                              # nothing is sent from here


async def test_an_upload_with_no_valid_rows_is_done_at_once(api):
    world = await api.seed_org()
    r = await upload(api, world, world.owner, sheet("nope", "also-nope"))
    assert (r.json()["rows_ok"], r.json()["rows_failed"]) == (0, 2)
    (batch,) = await table(api, InviteBatch)
    assert batch.status == "done" and batch.finished_at == NOW and await count(api, Job) == 0


async def test_errors_preview_is_the_first_20_and_all_errors_are_kept(api):
    world = await api.seed_org()
    r = await upload(api, world, world.owner, sheet(*[f"bad{i}" for i in range(45)]))
    body = r.json()
    assert body["rows_failed"] == 45 and len(body["errors_preview"]) == 20
    assert [e["row"] for e in body["errors_preview"]] == list(range(2, 22))
    (batch,) = await table(api, InviteBatch)
    assert len(batch.errors) == 45


async def test_filename_and_name_are_trimmed_and_kept_short(api):
    world = await api.seed_org()
    r = await upload(api, world, world.owner, f"email,name\na@amity.edu,{'N' * 500}\n", filename="f" * 400 + ".csv")
    assert r.status_code == 200
    (invite,) = [i for i in await table(api, Invite)]
    assert len(invite.invitee_name) == 200
    (batch,) = await table(api, InviteBatch)
    assert len(batch.filename) <= 200


async def test_csv_uploads_are_allowed_for_unpaid_seats_but_the_report_warns(api):
    for status, expected in (("active", False), ("past_due", True), ("suspended", True)):
        world = await api.seed_org(seat_status=status)
        r = await upload(api, world, world.owner, sheet(f"a-{status}@amity.edu", f"b-{status}@amity.edu"))
        assert r.status_code == 200 and r.json()["rows_ok"] == 2, status
        assert r.json()["seats"]["payment_due"] is expected, status
        dry = await upload(api, world, world.owner, sheet(f"c-{status}@amity.edu"), dry_run=True)
        assert dry.json()["seats"]["payment_due"] is expected, status
