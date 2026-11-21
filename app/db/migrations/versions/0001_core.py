"""core tables: organisations, memberships, seat_state, known_users, audit_log, outbox, processed_events,
idempotency_keys, jobs

Hand-written. Enum columns use CHECK constraints (values from app/core/enums.py), not Postgres ENUM types, so a value
can be added without ALTER TYPE. Ids are generated in Python (uuid7); times come from the Clock, so no defaults.
"""
from alembic import op

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS citext",
    """
    CREATE TABLE organisations (
        id uuid PRIMARY KEY,
        name text NOT NULL,
        slug text NOT NULL,
        type text NOT NULL,
        legal_name text,
        gstin text,
        state_code char(2),
        billing_email citext,
        status text NOT NULL,
        pilot_ends_at timestamptz,
        created_by uuid,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        closed_at timestamptz,
        CONSTRAINT uq_organisations_slug UNIQUE (slug),
        CONSTRAINT ck_organisations_type CHECK (type IN ('college', 'company')),
        CONSTRAINT ck_organisations_status
            CHECK (status IN ('pilot', 'active', 'pilot_expired', 'suspended', 'closed')),
        CONSTRAINT ck_organisations_state_code CHECK (state_code IS NULL OR state_code ~ '^[0-9]{2}$')
    )
    """,
    "CREATE INDEX ix_organisations_status ON organisations (status)",
    """
    CREATE TABLE memberships (
        id uuid PRIMARY KEY,
        org_id uuid NOT NULL REFERENCES organisations (id),
        user_id uuid NOT NULL,
        role text NOT NULL,
        status text NOT NULL,
        source text NOT NULL,
        invited_by uuid,
        joined_at timestamptz NOT NULL,
        ended_at timestamptz,
        end_reason text,
        CONSTRAINT ck_memberships_role CHECK (role IN ('owner', 'admin', 'member')),
        CONSTRAINT ck_memberships_status CHECK (status IN ('active', 'left', 'removed')),
        CONSTRAINT ck_memberships_source CHECK (source IN ('staff', 'invite', 'csv', 'link', 'domain')),
        CONSTRAINT ck_memberships_end_reason
            CHECK (end_reason IS NULL OR end_reason IN ('left', 'removed', 'user_deleted', 'org_closed')),
        CONSTRAINT ck_memberships_ended CHECK (
            (status = 'active' AND ended_at IS NULL AND end_reason IS NULL)
            OR (status <> 'active' AND ended_at IS NOT NULL AND end_reason IS NOT NULL)
        )
    )
    """,
    # Rule D1: a user is in at most one active organisation.
    "CREATE UNIQUE INDEX ux_memberships_one_active_org ON memberships (user_id) WHERE status = 'active'",
    "CREATE INDEX ix_memberships_org_status ON memberships (org_id, status)",
    # Counting active owners (last-owner rule) and keyset paging of the active member list.
    "CREATE INDEX ix_memberships_active_owners ON memberships (org_id) WHERE status = 'active' AND role = 'owner'",
    "CREATE INDEX ix_memberships_active_page ON memberships (org_id, joined_at, id) WHERE status = 'active'",
    """
    CREATE TABLE seat_state (
        org_id uuid PRIMARY KEY REFERENCES organisations (id),
        plan_code text NOT NULL,
        variant text NOT NULL,
        seats_bought integer,
        status text NOT NULL,
        period_end timestamptz,
        source text NOT NULL,
        source_ref text,
        refreshed_at timestamptz NOT NULL,
        CONSTRAINT ck_seat_state_plan_code CHECK (plan_code IN ('pilot', 'org_pro', 'org_max', 'payg')),
        CONSTRAINT ck_seat_state_variant CHECK (variant IN ('college', 'company')),
        CONSTRAINT ck_seat_state_source CHECK (source IN ('staff', 'billing')),
        -- NULL = no cap (Pay as you Go)
        CONSTRAINT ck_seat_state_seats_bought CHECK (seats_bought IS NULL OR seats_bought >= 1)
    )
    """,
    """
    CREATE TABLE known_users (
        user_id uuid PRIMARY KEY,
        email citext NOT NULL,
        full_name text,
        email_verified_at timestamptz,
        deleted_at timestamptz,
        updated_at timestamptz NOT NULL
    )
    """,
    "CREATE INDEX ix_known_users_email ON known_users (email)",
    """
    CREATE TABLE audit_log (
        id uuid PRIMARY KEY,
        org_id uuid,
        actor_id uuid,
        actor_kind text NOT NULL,
        action text NOT NULL,
        target_type text,
        target_id text,
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        reason text,
        correlation_id text,
        at timestamptz NOT NULL,
        CONSTRAINT ck_audit_log_actor_kind CHECK (actor_kind IN ('user', 'staff', 'service', 'system'))
    )
    """,
    "CREATE INDEX ix_audit_log_org_at ON audit_log (org_id, at DESC, id DESC)",
    "CREATE INDEX ix_audit_log_org_action ON audit_log (org_id, action)",
    """
    CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% on % is not allowed: the table is insert-only', TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END;
    $$
    """,
    """
    CREATE TRIGGER audit_log_insert_only BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_change()
    """,
    """
    CREATE TABLE outbox (
        id uuid PRIMARY KEY,
        topic text NOT NULL,
        key text NOT NULL,
        payload jsonb NOT NULL,
        created_at timestamptz NOT NULL,
        attempts integer NOT NULL DEFAULT 0,
        next_attempt_at timestamptz NOT NULL,
        published_at timestamptz,
        last_error text
    )
    """,
    "CREATE INDEX ix_outbox_unpublished ON outbox (next_attempt_at) WHERE published_at IS NULL",
    """
    CREATE TABLE processed_events (
        consumer text NOT NULL,
        event_id text NOT NULL,
        processed_at timestamptz NOT NULL,
        CONSTRAINT pk_processed_events PRIMARY KEY (consumer, event_id)
    )
    """,
    "CREATE INDEX ix_processed_events_processed_at ON processed_events (processed_at)",
    """
    CREATE TABLE idempotency_keys (
        key text NOT NULL,
        actor_id uuid NOT NULL,
        route text NOT NULL,
        request_hash text NOT NULL,
        status_code integer NOT NULL,
        response jsonb,
        created_at timestamptz NOT NULL,
        CONSTRAINT pk_idempotency_keys PRIMARY KEY (actor_id, route, key)
    )
    """,
    "CREATE INDEX ix_idempotency_keys_created_at ON idempotency_keys (created_at)",
    """
    CREATE TABLE jobs (
        id uuid PRIMARY KEY,
        kind text NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        run_at timestamptz NOT NULL,
        attempts integer NOT NULL DEFAULT 0,
        locked_until timestamptz,
        last_error text,
        done_at timestamptz,
        dedupe_key text
    )
    """,
    "CREATE INDEX ix_jobs_due ON jobs (run_at) WHERE done_at IS NULL",
    # A recurring job is never queued twice while one is still open.
    "CREATE UNIQUE INDEX ux_jobs_open_dedupe ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL AND done_at IS NULL",
]

DROPS = [
    "DROP TABLE IF EXISTS jobs",
    "DROP TABLE IF EXISTS idempotency_keys",
    "DROP TABLE IF EXISTS processed_events",
    "DROP TABLE IF EXISTS outbox",
    "DROP TRIGGER IF EXISTS audit_log_insert_only ON audit_log",
    "DROP FUNCTION IF EXISTS forbid_change()",
    "DROP TABLE IF EXISTS audit_log",
    "DROP TABLE IF EXISTS known_users",
    "DROP TABLE IF EXISTS seat_state",
    "DROP TABLE IF EXISTS memberships",
    "DROP TABLE IF EXISTS organisations",
    "DROP EXTENSION IF EXISTS citext",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROPS:
        op.execute(statement)
