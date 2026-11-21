"""org_domains, join_requests, org_policies"""
from alembic import op

revision = "0003_domains_policies"
down_revision = "0002_invites"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE org_domains (
        org_id uuid NOT NULL REFERENCES organisations (id),
        domain text NOT NULL,
        verify_token text NOT NULL,
        verified_at timestamptz,
        verified_by uuid,
        verify_method text,
        auto_join boolean NOT NULL DEFAULT false,
        CONSTRAINT pk_org_domains PRIMARY KEY (org_id, domain),
        CONSTRAINT ck_org_domains_lowercase CHECK (domain = lower(domain)),
        CONSTRAINT ck_org_domains_verify_method CHECK (verify_method IS NULL OR verify_method IN ('dns', 'staff')),
        CONSTRAINT ck_org_domains_verified CHECK ((verified_at IS NULL) = (verify_method IS NULL))
    )
    """,
    # A domain is verified for one organisation only.
    "CREATE UNIQUE INDEX ux_org_domains_verified ON org_domains (domain) WHERE verified_at IS NOT NULL",
    """
    CREATE TABLE join_requests (
        id uuid PRIMARY KEY,
        org_id uuid NOT NULL REFERENCES organisations (id),
        user_id uuid NOT NULL,
        domain text NOT NULL,
        status text NOT NULL,
        created_at timestamptz NOT NULL,
        decided_by uuid,
        decided_at timestamptz,
        CONSTRAINT ck_join_requests_status CHECK (status IN ('pending', 'approved', 'declined', 'expired'))
    )
    """,
    "CREATE INDEX ix_join_requests_org_status ON join_requests (org_id, status, created_at)",
    """
    CREATE TABLE org_policies (
        org_id uuid PRIMARY KEY REFERENCES organisations (id),
        lock_modes boolean NOT NULL DEFAULT false,
        approved_mode_ids uuid[] NOT NULL DEFAULT '{}',
        member_spend_cap_paise bigint,
        version integer NOT NULL DEFAULT 1,
        updated_by uuid,
        updated_at timestamptz NOT NULL,
        CONSTRAINT ck_org_policies_spend_cap CHECK (member_spend_cap_paise IS NULL OR member_spend_cap_paise >= 0),
        CONSTRAINT ck_org_policies_version CHECK (version >= 1)
    )
    """,
]

DROPS = ["DROP TABLE IF EXISTS org_policies", "DROP TABLE IF EXISTS join_requests", "DROP TABLE IF EXISTS org_domains"]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROPS:
        op.execute(statement)
