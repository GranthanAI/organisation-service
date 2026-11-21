"""invites and invite_batches"""
from alembic import op

revision = "0002_invites"
down_revision = "0001_core"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE invite_batches (
        id uuid PRIMARY KEY,
        org_id uuid NOT NULL REFERENCES organisations (id),
        uploaded_by uuid,
        filename text,
        rows_total integer NOT NULL DEFAULT 0,
        rows_ok integer NOT NULL DEFAULT 0,
        rows_failed integer NOT NULL DEFAULT 0,
        status text NOT NULL,
        errors jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL,
        finished_at timestamptz,
        CONSTRAINT ck_invite_batches_status CHECK (status IN ('validating', 'queued', 'sending', 'done', 'failed')),
        CONSTRAINT ck_invite_batches_errors_max
            CHECK (jsonb_typeof(errors) = 'array' AND jsonb_array_length(errors) <= 5000)
    )
    """,
    "CREATE INDEX ix_invite_batches_org ON invite_batches (org_id, created_at)",
    """
    CREATE TABLE invites (
        id uuid PRIMARY KEY,
        org_id uuid NOT NULL REFERENCES organisations (id),
        kind text NOT NULL,
        email citext,
        role text NOT NULL,
        token_hash text NOT NULL,
        max_uses integer,
        uses integer NOT NULL DEFAULT 0,
        batch_id uuid REFERENCES invite_batches (id),
        domain_restricted boolean NOT NULL DEFAULT false,
        expires_at timestamptz NOT NULL,
        accepted_at timestamptz,
        accepted_by uuid,
        revoked_at timestamptz,
        created_by uuid,
        created_at timestamptz NOT NULL,
        CONSTRAINT uq_invites_token_hash UNIQUE (token_hash),
        CONSTRAINT ck_invites_kind CHECK (kind IN ('email', 'csv', 'link')),
        CONSTRAINT ck_invites_role CHECK (role IN ('owner', 'admin', 'member')),
        -- email is NULL for links; the 90-day purge of never-accepted invites also nulls it
        CONSTRAINT ck_invites_link_has_no_email CHECK (kind <> 'link' OR email IS NULL),
        CONSTRAINT ck_invites_max_uses CHECK (max_uses IS NULL OR max_uses >= 1),
        CONSTRAINT ck_invites_uses CHECK (uses >= 0)
    )
    """,
    # One open invite per email per organisation.
    """
    CREATE UNIQUE INDEX ux_invites_open_email ON invites (org_id, email)
        WHERE kind <> 'link' AND accepted_at IS NULL AND revoked_at IS NULL
    """,
    "CREATE INDEX ix_invites_org_created ON invites (org_id, created_at DESC, id DESC)",
    "CREATE INDEX ix_invites_batch ON invites (batch_id) WHERE batch_id IS NOT NULL",
    "CREATE INDEX ix_invites_email ON invites (email) WHERE email IS NOT NULL",
]

DROPS = ["DROP TABLE IF EXISTS invites", "DROP TABLE IF EXISTS invite_batches"]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROPS:
        op.execute(statement)
