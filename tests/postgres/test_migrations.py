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
    "ux_org_domains_verified", "ix_join_requests_org_status",
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
    assert fetch(empty_db, "SELECT version_num FROM alembic_version")[0]["version_num"] == "0004_grants"

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
