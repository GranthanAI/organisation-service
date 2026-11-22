"""GET /org/v1/invite-batches/{id} and /errors.csv; plus the PHASES exit check: college_2000.csv uploads in under 5 s."""

import csv
import io
import time
import uuid
from pathlib import Path

import pytest

from tests.support.factories import NOW

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "college_2000.csv"


async def upload(api, world, who, text, dry_run=None):
    data = {} if dry_run is None else {"dry_run": str(dry_run).lower()}
    return await api.client.post(f"/org/v1/orgs/{world.org.id}/invites/csv", files={"file": ("s.csv", text.encode(), "text/csv")},
                                 data=data, headers={**api.h(who), "Idempotency-Key": uuid.uuid4().hex})


async def status(api, batch_id, who):
    return await api.client.get(f"/org/v1/invite-batches/{batch_id}", headers=api.h(who))


async def errors_csv(api, batch_id, who):
    return await api.client.get(f"/org/v1/invite-batches/{batch_id}/errors.csv", headers=api.h(who))


async def test_status_shape_and_progress(api):
    world = await api.seed_org()
    batch_id = (await upload(api, world, world.admin, "email\na@amity.edu\nb@amity.edu\nc@amity.edu\nbad\n")).json()["batch_id"]
    r = await status(api, batch_id, world.admin)
    assert r.status_code == 200
    assert r.json() == {"batch_id": batch_id, "status": "sending", "dry_run": False, "rows_total": 4, "rows_ok": 3,
                        "rows_failed": 1, "sent": 0, "pending_send": 3, "not_sent": 0, "errors_note": None, "created_at": r.json()["created_at"], "finished_at": None}
    assert r.json()["created_at"].startswith(NOW.strftime("%Y-%m-%dT%H:%M"))
    await api.run_jobs()                                                     # the emails go out
    r = (await status(api, batch_id, world.owner)).json()
    assert (r["sent"], r["pending_send"], r["status"]) == (3, 0, "sending")   # done only after the minute job
    from app.db.session import unit_of_work
    from app.services import invite_jobs
    async with unit_of_work(api.sf) as s:
        await invite_jobs.csv_batches_finish(s, None, api.clock)
    done = (await status(api, batch_id, world.owner)).json()
    assert done["status"] == "done" and done["finished_at"] is not None


async def test_a_dry_run_batch_reports_done_with_nothing_sent(api):
    world = await api.seed_org()
    batch_id = (await upload(api, world, world.owner, "email\na@amity.edu\n", dry_run=True)).json()["batch_id"]
    r = (await status(api, batch_id, world.owner)).json()
    assert (r["status"], r["dry_run"], r["sent"], r["pending_send"], r["rows_ok"]) == ("done", True, 0, 0, 1)


async def test_the_other_orgs_batch_is_404_and_a_plain_member_is_403(api):
    world, other = await api.seed_org(), await api.seed_org()
    batch_id = (await upload(api, world, world.owner, "email\nbad\n")).json()["batch_id"]
    for who in (other.owner, other.admin):
        for route in (status, errors_csv):
            r = await route(api, batch_id, who)
            assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert (await status(api, uuid.uuid4(), world.owner)).status_code == 404
    assert (await status(api, batch_id, world.members[0])).status_code == 403
    assert (await errors_csv(api, batch_id, world.members[0])).status_code == 403
    assert (await api.client.get(f"/org/v1/invite-batches/{batch_id}")).status_code == 401
    assert (await status(api, batch_id, world.admin)).status_code == 200


async def test_errors_csv_headers_bom_and_content(api):
    world = await api.seed_org()
    batch_id = (await upload(api, world, world.owner, "email\na@amity.edu\nnot-an-email\na@amity.edu\n")).json()["batch_id"]
    r = await errors_csv(api, batch_id, world.owner)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == f'attachment; filename="invite-errors-{batch_id}.csv"'
    assert r.content.startswith(b"\xef\xbb\xbf")                              # the BOM: Excel opens it as UTF-8
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows == [["row", "email", "reason"], ["3", "not-an-email", "invalid_email"], ["4", "a@amity.edu", "duplicate_in_file"]]
    assert r.content.endswith(b"\r\n")


DANGEROUS = ["=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(A1:A9)", "\t=1", "\r=1"]


async def test_errors_csv_neutralises_every_dangerous_cell(api):
    world = await api.seed_org()
    text = "email\n" + "".join(f'"{c}"\n' for c in DANGEROUS) + "plain-bad\n"
    batch_id = (await upload(api, world, world.owner, text)).json()["batch_id"]
    r = await errors_csv(api, batch_id, world.owner)
    parsed = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"), newline="")))[1:]
    cells = [row[1] for row in parsed]
    for original in DANGEROUS:
        stripped = original.strip()                                          # the service trims the cell before reporting it
        assert any(c in ("'" + stripped, stripped) for c in cells), original
    for cell in cells:
        assert not cell.startswith(("=", "+", "-", "@", "\t", "\r")), cell    # no cell can start a formula
    assert [c for c in cells if c.startswith("'")] and "plain-bad" in cells
    assert all(row[0].isdigit() and row[2] == "invalid_email" for row in parsed)


async def test_errors_csv_is_empty_but_valid_when_nothing_failed(api):
    world = await api.seed_org()
    batch_id = (await upload(api, world, world.owner, "email\na@amity.edu\n")).json()["batch_id"]
    r = await errors_csv(api, batch_id, world.owner)
    assert r.content.decode("utf-8-sig") == "row,email,reason\r\n"


async def test_errors_csv_after_the_purge_is_just_the_header(api):
    world = await api.seed_org()
    batch_id = (await upload(api, world, world.owner, "email\nbad\n")).json()["batch_id"]
    from datetime import timedelta

    from app.db.session import unit_of_work
    from app.services import invite_jobs
    api.clock.advance(seconds=int(timedelta(days=91).total_seconds()))
    async with unit_of_work(api.sf) as s:
        await invite_jobs.invites_purge(s, None, api.clock)
    r = await errors_csv(api, batch_id, world.owner)
    assert r.status_code == 200 and r.content.decode("utf-8-sig") == "row,email,reason\r\n"
    assert (await status(api, batch_id, world.owner)).json()["rows_failed"] == 1       # the counts stay


@pytest.fixture
async def big_org(api):
    return await api.seed_org(seats=5000)


async def test_college_2000_uploads_in_under_5_seconds_through_the_api(api, big_org):
    data = FIXTURE.read_bytes()
    started = time.perf_counter()
    r = await api.client.post(f"/org/v1/orgs/{big_org.org.id}/invites/csv", files={"file": ("college_2000.csv", data, "text/csv")},
                              headers={**api.h(big_org.owner), "Idempotency-Key": uuid.uuid4().hex})
    elapsed = time.perf_counter() - started
    print(f"\nCOLLEGE_2000_UPLOAD_SECONDS={elapsed:.2f}")
    assert r.status_code == 200
    body = r.json()
    assert (body["rows_total"], body["rows_ok"], body["rows_failed"]) == (2000, 1950, 50) and len(body["errors_preview"]) == 20
    assert elapsed < 5.0, f"the upload took {elapsed:.2f} s"
    # the error file of the same batch: every dangerous-looking cell starts with an apostrophe, none starts a formula
    rows = list(csv.reader(io.StringIO((await errors_csv(api, body["batch_id"], big_org.owner)).content.decode("utf-8-sig"))))
    assert len(rows) == 51 and all(not c.startswith(("=", "+", "-", "@", "\t", "\r")) for row in rows for c in row)
