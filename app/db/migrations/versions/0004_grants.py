"""grants for the runtime role org_app (DML only; audit_log is insert/select only)

Skipped silently when the role does not exist (dev connects as postgres). The forbid_change() trigger is the guard
everywhere, with or without this grant.
"""
from alembic import op

revision = "0004_grants"
down_revision = "0003_domains_policies"
branch_labels = None
depends_on = None

UPGRADE = """
DO $$
DECLARE t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'org_app') THEN
        FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version' LOOP
            IF t = 'audit_log' THEN
                EXECUTE format('GRANT SELECT, INSERT ON %I TO org_app', t);
            ELSE
                EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO org_app', t);
            END IF;
        END LOOP;
    END IF;
END
$$
"""

DOWNGRADE = """
DO $$
DECLARE t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'org_app') THEN
        FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version' LOOP
            EXECUTE format('REVOKE ALL ON %I FROM org_app', t);
        END LOOP;
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
