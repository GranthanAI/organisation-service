"""CSV bulk invites and invite links: the invitee's name, and a dry-run flag on batches.

invites.invitee_name: the `name` column of the uploaded CSV (an email address's owner), purged together with the email.
invite_batches.dry_run: a validation-only upload writes only its batch row (with the errors); this marks it.

No new index: the batch queries (progress, finish job) go through `ix_invites_batch`, which Phase 2 already created, and
the daily CSV row count is a range scan on `ix_invites_org_created`. See reports/P8-report.md for the EXPLAIN output.
"""
from alembic import op

revision = "0008_csv_and_links"
down_revision = "0007_invite_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE invites ADD COLUMN invitee_name text")
    op.execute("ALTER TABLE invite_batches ADD COLUMN dry_run boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE invite_batches DROP COLUMN dry_run")
    op.execute("ALTER TABLE invites DROP COLUMN invitee_name")
