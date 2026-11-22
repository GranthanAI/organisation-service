"""Email domains (Phase 9) and the "stopped" end of a CSV batch (O3).

org_domains.include_subdomains: a verified domain also matches its subdomains (cse.amity.edu for amity.edu).
org_domains.added_at: when the domain was added (the hourly check stops after 7 days).
org_domains.failed_checks / last_checked_at: the 30-day DNS re-check; 3 failures in a row remove the verification.
invite_batches.status may now be 'stopped' (every invite still unsent can no longer be sent), with errors_note saying why.

`ck_org_domains_verify_method` (dns or staff) already exists since 0003, so it is not touched. No new index: the
verified-domain lookup by name is served by ux_org_domains_verified (see reports/P9-report.md for the EXPLAIN).
"""
from alembic import op

revision = "0009_domains"
down_revision = "0008_csv_and_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE org_domains ADD COLUMN include_subdomains boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE org_domains ADD COLUMN failed_checks integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE org_domains ADD COLUMN last_checked_at timestamptz")
    op.execute("ALTER TABLE org_domains ADD COLUMN added_at timestamptz NOT NULL DEFAULT now()")      # existing rows: now
    op.execute("ALTER TABLE org_domains ADD CONSTRAINT ck_org_domains_failed_checks CHECK (failed_checks >= 0)")
    op.execute("ALTER TABLE invite_batches ADD COLUMN errors_note text")
    op.execute("ALTER TABLE invite_batches DROP CONSTRAINT ck_invite_batches_status")
    op.execute("ALTER TABLE invite_batches ADD CONSTRAINT ck_invite_batches_status "
               "CHECK (status IN ('validating', 'queued', 'sending', 'done', 'failed', 'stopped'))")


def downgrade() -> None:
    op.execute("UPDATE invite_batches SET status = 'done' WHERE status = 'stopped'")   # the old CHECK has no 'stopped'
    op.execute("ALTER TABLE invite_batches DROP CONSTRAINT ck_invite_batches_status")
    op.execute("ALTER TABLE invite_batches ADD CONSTRAINT ck_invite_batches_status "
               "CHECK (status IN ('validating', 'queued', 'sending', 'done', 'failed'))")
    op.execute("ALTER TABLE invite_batches DROP COLUMN errors_note")
    op.execute("ALTER TABLE org_domains DROP CONSTRAINT ck_org_domains_failed_checks")
    op.execute("ALTER TABLE org_domains DROP COLUMN added_at")
    op.execute("ALTER TABLE org_domains DROP COLUMN last_checked_at")
    op.execute("ALTER TABLE org_domains DROP COLUMN failed_checks")
    op.execute("ALTER TABLE org_domains DROP COLUMN include_subdomains")
