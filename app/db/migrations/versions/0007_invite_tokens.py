"""invite tokens are derived, not random: version, resend count, last sent, purged.

invites.token_version: the link token is HMAC(secret, "invite:<id>:<token_version>"); a resend bumps it.
invites.resend_count: how many times staff or admins have resent it (at most 3).
invites.last_sent_at: when the email was last sent.
invites.purged_at: when the email address was removed from a never-accepted invite after 90 days.

No CHECK requires an email for email or csv invites (only "a link has none"), so there is nothing to relax: a purged invite
simply has email NULL. Invites created before this migration keep their old random token_hash, which no link can match;
they get a derived hash at their first send or resend.
"""
from alembic import op

revision = "0007_invite_tokens"
down_revision = "0006_outbox_correlation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE invites ADD COLUMN token_version integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE invites ADD COLUMN resend_count integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE invites ADD COLUMN last_sent_at timestamptz")
    op.execute("ALTER TABLE invites ADD COLUMN purged_at timestamptz")
    op.execute("ALTER TABLE invites ADD CONSTRAINT ck_invites_token_version CHECK (token_version >= 1)")
    op.execute("ALTER TABLE invites ADD CONSTRAINT ck_invites_resend_count CHECK (resend_count >= 0)")


def downgrade() -> None:
    op.execute("ALTER TABLE invites DROP CONSTRAINT ck_invites_resend_count")
    op.execute("ALTER TABLE invites DROP CONSTRAINT ck_invites_token_version")
    op.execute("ALTER TABLE invites DROP COLUMN purged_at")
    op.execute("ALTER TABLE invites DROP COLUMN last_sent_at")
    op.execute("ALTER TABLE invites DROP COLUMN resend_count")
    op.execute("ALTER TABLE invites DROP COLUMN token_version")
