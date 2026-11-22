"""upgrade -> downgrade to base -> upgrade again on an empty throwaway database; the schema has what lld.md says."""

import asyncio

import asyncpg
import pytest

from app.db.base import Base
from tests.support import db as testdb

TABLES = {
    "organisations", "memberships", "seat_state", "known_users", "audit_log", "outbox", "processed_events",
    "idempotency_keys", "jobs", "invites", "invite_batches", "org_domains", "join_requests", "org_policies",
}
INDEXES = {
    "ux_memberships_one_active_org", "ix_memberships_org_status", "ix_memberships_active_owners",
    "ix_memberships_active_page", "ix_organisations_status", "ix_known_users_email", "ix_audit_log_org_at",
    "ix_audit_log_org_action", "ix_outbox_unpublished", "ix_processed_events_processed_at",
    "ix_idempotency_keys_created_at", "ix_jobs_due", "ux_jobs_open_dedupe", "ux_invites_open_email",
    "ix_invites_org_created", "ix_invites_batch", "ix_invites_email", "ix_invite_batches_org",
    "ux_org_domains_verified", "ix_join_requests_org_status", "ux_join_requests_pending",
}


def fetch(url: str, sql: str, *args):
    async def go():
        conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


def tables(url):
    rows = fetch(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {r["tablename"] for r in rows}


@pytest.fixture
def empty_db():
    name = testdb.new_database_name()
    testdb.create_database(name)
    try:
        yield testdb.url_for(name)
    finally:
        testdb.drop_database(name)


def test_upgrade_downgrade_upgrade(empty_db):
    testdb.upgrade(empty_db)
    assert tables(empty_db) == TABLES | {"alembic_version"}
    assert fetch(empty_db, "SELECT version_num FROM alembic_version")[0]["version_num"] == "0010_pending_seats"

    testdb.downgrade(empty_db, "base")
    assert tables(empty_db) == {"alembic_version"}
    assert not fetch(empty_db, "SELECT 1 FROM pg_extension WHERE extname = 'citext'")
    assert not fetch(empty_db, "SELECT 1 FROM pg_proc WHERE proname = 'forbid_change'")

    testdb.upgrade(empty_db)
    assert tables(empty_db) == TABLES | {"alembic_version"}


def test_each_migration_downgrades_one_step_at_a_time(empty_db):
    steps = [("0001_core", {"organisations", "memberships", "seat_state", "known_users", "audit_log", "outbox",
                            "processed_events", "idempotency_keys", "jobs"}),
             ("0002_invites", None), ("0003_domains_policies", None), ("0004_grants", None)]
    testdb.upgrade(empty_db, "0001_core")
    assert tables(empty_db) - {"alembic_version"} == steps[0][1]
    testdb.upgrade(empty_db, "0002_invites")
    assert {"invites", "invite_batches"} <= tables(empty_db)
    testdb.upgrade(empty_db, "0003_domains_policies")
    assert {"org_domains", "join_requests", "org_policies"} <= tables(empty_db)
    testdb.upgrade(empty_db, "head")
    testdb.downgrade(empty_db, "0004_grants")
    testdb.downgrade(empty_db, "0003_domains_policies")
    testdb.downgrade(empty_db, "0002_invites")
    assert not ({"org_domains", "join_requests", "org_policies"} & tables(empty_db))
    testdb.downgrade(empty_db, "0001_core")
    assert not ({"invites", "invite_batches"} & tables(empty_db))
    testdb.downgrade(empty_db, "base")
    assert tables(empty_db) == {"alembic_version"}


def test_named_indexes_exist(empty_db):
    testdb.upgrade(empty_db)
    names = {r["indexname"] for r in fetch(empty_db, "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert INDEXES <= names


def test_partial_indexes_are_partial_and_unique_where_promised(empty_db):
    testdb.upgrade(empty_db)
    rows = fetch(empty_db, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
    defs = {r["indexname"]: r["indexdef"] for r in rows}
    assert "UNIQUE" in defs["ux_memberships_one_active_org"] and "(user_id)" in defs["ux_memberships_one_active_org"]
    assert "status = 'active'" in defs["ux_memberships_one_active_org"]
    assert "role = 'owner'" in defs["ix_memberships_active_owners"]
    assert "published_at IS NULL" in defs["ix_outbox_unpublished"]
    assert "done_at IS NULL" in defs["ix_jobs_due"] and "done_at IS NULL" in defs["ux_jobs_open_dedupe"]
    assert "UNIQUE" in defs["ux_org_domains_verified"] and "verified_at IS NOT NULL" in defs["ux_org_domains_verified"]
    assert "UNIQUE" in defs["ux_invites_open_email"] and "revoked_at IS NULL" in defs["ux_invites_open_email"]


def test_composite_primary_keys(empty_db):
    testdb.upgrade(empty_db)
    sql = ("SELECT array_agg(a.attname ORDER BY k.ord) AS cols FROM pg_index i "
           "JOIN pg_class c ON c.oid = i.indrelid "
           "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
           "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
           "WHERE i.indisprimary AND c.relname = $1")
    assert fetch(empty_db, sql, "processed_events")[0]["cols"] == ["consumer", "event_id"]
    assert fetch(empty_db, sql, "idempotency_keys")[0]["cols"] == ["actor_id", "route", "key"]
    assert fetch(empty_db, sql, "seat_state")[0]["cols"] == ["org_id"]


def test_models_match_the_migrated_schema(empty_db):
    """Every model table and column exists in the database with the same nullability (the models are not used to
    create the schema, so this catches drift)."""
    import app.models  # noqa: F401

    testdb.upgrade(empty_db)
    cols = fetch(empty_db, "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
                           "WHERE table_schema = 'public' AND table_name <> 'alembic_version'")
    actual = {(r["table_name"], r["column_name"]): r["is_nullable"] == "YES" for r in cols}
    expected = {(t.name, c.name): c.nullable for t in Base.metadata.tables.values() for c in t.columns}
    assert set(Base.metadata.tables) == TABLES
    assert expected == actual


def columns(url, table):
    rows = fetch(url, "SELECT column_name, is_nullable FROM information_schema.columns "
                      "WHERE table_schema = 'public' AND table_name = $1", table)
    return {r["column_name"]: r["is_nullable"] == "YES" for r in rows}


def test_0005_up_down_up(empty_db):
    testdb.upgrade(empty_db, "0004_grants")
    before = columns(empty_db, "idempotency_keys")
    assert before["status_code"] is False and "state" not in before and "revoke_reason" not in columns(empty_db, "invites")

    testdb.upgrade(empty_db, "0005_plumbing_fixes")
    idem = columns(empty_db, "idempotency_keys")
    assert idem["status_code"] is True and idem["response"] is True and idem["locked_until"] is True
    assert idem["state"] is False
    assert columns(empty_db, "invites")["revoke_reason"] is True
    defs = {r["indexname"]: r["indexdef"] for r in fetch(empty_db, "SELECT indexname, indexdef FROM pg_indexes")}
    assert "status = 'pending'" in defs["ux_join_requests_pending"] and "UNIQUE" in defs["ux_join_requests_pending"]

    testdb.downgrade(empty_db, "0004_grants")
    assert columns(empty_db, "idempotency_keys") == before
    assert "revoke_reason" not in columns(empty_db, "invites")
    assert "ux_join_requests_pending" not in {r["indexname"] for r in fetch(empty_db, "SELECT indexname FROM pg_indexes")}

    testdb.upgrade(empty_db)
    assert columns(empty_db, "idempotency_keys")["state"] is False


def test_0005_downgrade_drops_reservations_and_upgrade_backfills_revoked_invites(empty_db):
    testdb.upgrade(empty_db, "0004_grants")

    async def go():
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            org = "00000000-0000-7000-8000-000000000001"
            await conn.execute("INSERT INTO organisations (id, name, slug, type, status, created_at, updated_at) "
                               "VALUES ($1, 'o', 'o', 'college', 'active', now(), now())", __import__("uuid").UUID(org))
            await conn.execute("INSERT INTO invites (id, org_id, kind, email, role, token_hash, expires_at, revoked_at, "
                               "created_at) VALUES (gen_random_uuid(), $1, 'email', 'a@b.co', 'member', 'h', now(), now(), now())",
                               __import__("uuid").UUID(org))
        finally:
            await conn.close()

    asyncio.run(go())
    testdb.upgrade(empty_db)
    assert fetch(empty_db, "SELECT revoke_reason FROM invites")[0]["revoke_reason"] == "revoked"
    # a reservation (no status yet) is dropped on the way down, a finished row survives
    fetch(empty_db, "INSERT INTO idempotency_keys (key, actor_id, route, request_hash, state, created_at) "
                    "VALUES ('k1', gen_random_uuid(), 'r', 'h', 'in_progress', now()) RETURNING 1")
    fetch(empty_db, "INSERT INTO idempotency_keys (key, actor_id, route, request_hash, state, status_code, created_at) "
                    "VALUES ('k2', gen_random_uuid(), 'r', 'h', 'done', 201, now()) RETURNING 1")
    testdb.downgrade(empty_db, "0004_grants")
    assert [r["key"] for r in fetch(empty_db, "SELECT key FROM idempotency_keys")] == ["k2"]


def test_0006_adds_and_drops_the_outbox_correlation_column(empty_db):
    testdb.upgrade(empty_db, "0005_plumbing_fixes")
    assert "correlation_id" not in columns(empty_db, "outbox")
    testdb.upgrade(empty_db)
    assert columns(empty_db, "outbox")["correlation_id"] is True
    testdb.downgrade(empty_db, "0005_plumbing_fixes")
    assert "correlation_id" not in columns(empty_db, "outbox")
    testdb.upgrade(empty_db)
    assert columns(empty_db, "outbox")["correlation_id"] is True


def test_0007_up_down_up_and_existing_invites_get_defaults(empty_db):
    testdb.upgrade(empty_db, "0006_outbox_correlation")
    assert "token_version" not in columns(empty_db, "invites")

    async def seed():
        import uuid as _uuid
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            org = _uuid.uuid4()
            await conn.execute("INSERT INTO organisations (id, name, slug, type, status, created_at, updated_at) "
                               "VALUES ($1, 'o', 'o', 'college', 'active', now(), now())", org)
            await conn.execute("INSERT INTO invites (id, org_id, kind, email, role, token_hash, expires_at, created_at) "
                               "VALUES (gen_random_uuid(), $1, 'email', 'a@b.co', 'owner', 'oldhash', now(), now())", org)
        finally:
            await conn.close()

    asyncio.run(seed())
    testdb.upgrade(empty_db, "0007_invite_tokens")
    cols = columns(empty_db, "invites")
    assert cols["token_version"] is False and cols["resend_count"] is False          # NOT NULL with defaults
    assert cols["last_sent_at"] is True and cols["purged_at"] is True
    (row,) = fetch(empty_db, "SELECT token_version, resend_count, last_sent_at, purged_at, token_hash FROM invites")
    assert (row["token_version"], row["resend_count"], row["last_sent_at"], row["purged_at"]) == (1, 0, None, None)
    assert row["token_hash"] == "oldhash"                                              # fixed at its first send or resend

    testdb.downgrade(empty_db, "0006_outbox_correlation")
    assert not ({"token_version", "resend_count", "last_sent_at", "purged_at"} & set(columns(empty_db, "invites")))
    assert fetch(empty_db, "SELECT count(*) AS n FROM invites")[0]["n"] == 1             # the data survives the downgrade
    testdb.upgrade(empty_db)
    assert columns(empty_db, "invites")["token_version"] is False


def test_0007_rejects_nonsense_values(empty_db):
    testdb.upgrade(empty_db)

    async def attempt(sql):
        import uuid as _uuid
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            org = _uuid.uuid4()
            await conn.execute("INSERT INTO organisations (id, name, slug, type, status, created_at, updated_at) "
                               "VALUES ($1, 'o', $2, 'college', 'active', now(), now())", org, f"o-{org.hex}")
            await conn.execute(sql, org)
        finally:
            await conn.close()

    base = ("INSERT INTO invites (id, org_id, kind, email, role, token_hash, expires_at, created_at, {col}) "
            "VALUES (gen_random_uuid(), $1, 'email', 'a@b.co', 'member', 'h', now(), now(), {val})")
    for col, val in (("token_version", 0), ("resend_count", -1)):
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(attempt(base.format(col=col, val=val)))


def test_0008_up_down_up_keeps_data_and_defaults(empty_db):
    testdb.upgrade(empty_db, "0007_invite_tokens")
    assert "invitee_name" not in columns(empty_db, "invites") and "dry_run" not in columns(empty_db, "invite_batches")

    async def seed():
        import uuid as _uuid
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            org = _uuid.uuid4()
            await conn.execute("INSERT INTO organisations (id, name, slug, type, status, created_at, updated_at) "
                               "VALUES ($1, 'o', 'o8', 'college', 'active', now(), now())", org)
            await conn.execute("INSERT INTO invite_batches (id, org_id, status, created_at) "
                               "VALUES (gen_random_uuid(), $1, 'done', now())", org)
            await conn.execute("INSERT INTO invites (id, org_id, kind, email, role, token_hash, expires_at, created_at) "
                               "VALUES (gen_random_uuid(), $1, 'csv', 'a@b.co', 'member', 'h8', now(), now())", org)
        finally:
            await conn.close()

    asyncio.run(seed())
    testdb.upgrade(empty_db, "0008_csv_and_links")
    assert columns(empty_db, "invites")["invitee_name"] is True                            # nullable
    assert columns(empty_db, "invite_batches")["dry_run"] is False                         # NOT NULL with a default
    assert fetch(empty_db, "SELECT dry_run FROM invite_batches")[0]["dry_run"] is False
    assert fetch(empty_db, "SELECT invitee_name FROM invites")[0]["invitee_name"] is None

    testdb.downgrade(empty_db, "0007_invite_tokens")
    assert "invitee_name" not in columns(empty_db, "invites") and "dry_run" not in columns(empty_db, "invite_batches")
    assert fetch(empty_db, "SELECT count(*) AS n FROM invites")[0]["n"] == 1               # the data survives the downgrade
    testdb.upgrade(empty_db)
    assert columns(empty_db, "invites")["invitee_name"] is True and columns(empty_db, "invite_batches")["dry_run"] is False


def test_0009_up_down_up_keeps_data_and_maps_stopped_back(empty_db):
    testdb.upgrade(empty_db, "0008_csv_and_links")
    assert "include_subdomains" not in columns(empty_db, "org_domains") and "errors_note" not in columns(empty_db, "invite_batches")

    async def seed():
        import uuid as _uuid
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            org = _uuid.uuid4()
            await conn.execute("INSERT INTO organisations (id, name, slug, type, status, created_at, updated_at) "
                               "VALUES ($1, 'o', 'o9', 'college', 'active', now(), now())", org)
            await conn.execute("INSERT INTO org_domains (org_id, domain, verify_token, verified_at, verify_method) "
                               "VALUES ($1, 'amity.edu', 'tok', now(), 'dns')", org)
            await conn.execute("INSERT INTO invite_batches (id, org_id, status, created_at) "
                               "VALUES (gen_random_uuid(), $1, 'sending', now())", org)
            with pytest.raises(asyncpg.CheckViolationError):                      # 'stopped' is not allowed before 0009
                await conn.execute("INSERT INTO invite_batches (id, org_id, status, created_at) "
                                   "VALUES (gen_random_uuid(), $1, 'stopped', now())", org)
        finally:
            await conn.close()

    asyncio.run(seed())
    testdb.upgrade(empty_db, "0009_domains")
    cols = columns(empty_db, "org_domains")
    assert cols["include_subdomains"] is False and cols["failed_checks"] is False and cols["last_checked_at"] is True
    assert cols["added_at"] is False
    (row,) = fetch(empty_db, "SELECT include_subdomains, failed_checks, last_checked_at, auto_join FROM org_domains")
    assert (row["include_subdomains"], row["failed_checks"], row["last_checked_at"], row["auto_join"]) == (False, 0, None, False)
    assert columns(empty_db, "invite_batches")["errors_note"] is True

    async def stop_one():
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await conn.execute("UPDATE invite_batches SET status = 'stopped', errors_note = 'org inactive'")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE org_domains SET failed_checks = -1")
        finally:
            await conn.close()

    asyncio.run(stop_one())
    testdb.downgrade(empty_db, "0008_csv_and_links")
    assert not ({"include_subdomains", "failed_checks", "last_checked_at", "added_at"} & set(columns(empty_db, "org_domains")))
    assert "errors_note" not in columns(empty_db, "invite_batches")
    assert fetch(empty_db, "SELECT status FROM invite_batches")[0]["status"] == "done"      # stopped maps back to done
    assert fetch(empty_db, "SELECT count(*) AS n FROM org_domains")[0]["n"] == 1             # the data survives
    testdb.upgrade(empty_db)
    assert columns(empty_db, "org_domains")["include_subdomains"] is False


def test_0010_up_down_up_keeps_seats_and_rejects_nonsense(empty_db):
    testdb.upgrade(empty_db, "0009_domains")
    assert "pending_seats" not in columns(empty_db, "seat_state")

    async def seed():
        import uuid as _uuid
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            org = _uuid.uuid4()
            await conn.execute("INSERT INTO organisations (id, name, slug, type, status, created_at, updated_at) "
                               "VALUES ($1, 'o', 'o10', 'college', 'active', now(), now())", org)
            await conn.execute("INSERT INTO seat_state (org_id, plan_code, variant, seats_bought, status, source, refreshed_at) "
                               "VALUES ($1, 'org_pro', 'college', 60, 'active', 'staff', now())", org)
            return org
        finally:
            await conn.close()

    org = asyncio.run(seed())
    testdb.upgrade(empty_db, "0010_pending_seats")
    assert columns(empty_db, "seat_state")["pending_seats"] is True
    assert fetch(empty_db, "SELECT pending_seats, seats_bought FROM seat_state")[0]["pending_seats"] is None

    async def bad():
        conn = await asyncpg.connect(empty_db.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await conn.execute("UPDATE seat_state SET pending_seats = 40 WHERE org_id = $1", org)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE seat_state SET pending_seats = -1 WHERE org_id = $1", org)
        finally:
            await conn.close()

    asyncio.run(bad())
    testdb.downgrade(empty_db, "0009_domains")
    assert "pending_seats" not in columns(empty_db, "seat_state")
    assert fetch(empty_db, "SELECT seats_bought FROM seat_state")[0]["seats_bought"] == 60
    testdb.upgrade(empty_db)
    assert columns(empty_db, "seat_state")["pending_seats"] is True
