"""plumbing fixes before Phase 3

H1 idempotency_keys: status_code and response nullable (a reservation has neither yet); add state and locked_until.
H2 join_requests: one pending request per (org, user).
H3 seat_state.status: CHECK with billing's subscription statuses.
H4 invites: revoke_reason, set together with revoked_at (an expired invite is closed by revoking it with reason
   'expired', which frees ux_invites_open_email).
"""
from alembic import op

revision = "0005_plumbing_fixes"
down_revision = "0004_grants"
branch_labels = None
depends_on = None

STATEMENTS = [
    # H1
    "ALTER TABLE idempotency_keys ALTER COLUMN status_code DROP NOT NULL",
    "ALTER TABLE idempotency_keys ADD COLUMN state text NOT NULL DEFAULT 'done'",
    "ALTER TABLE idempotency_keys ALTER COLUMN state DROP DEFAULT",
    "ALTER TABLE idempotency_keys ADD COLUMN locked_until timestamptz",
    "ALTER TABLE idempotency_keys ADD CONSTRAINT ck_idempotency_keys_state CHECK (state IN ('in_progress', 'done'))",
    # H2
    "CREATE UNIQUE INDEX ux_join_requests_pending ON join_requests (org_id, user_id) WHERE status = 'pending'",
    # H3
    """
    ALTER TABLE seat_state ADD CONSTRAINT ck_seat_state_status
        CHECK (status IN ('incomplete', 'active', 'past_due', 'suspended', 'cancelling', 'ended'))
    """,
    # H4
    "ALTER TABLE invites ADD COLUMN revoke_reason text",
    "UPDATE invites SET revoke_reason = 'revoked' WHERE revoked_at IS NOT NULL",
    """
    ALTER TABLE invites ADD CONSTRAINT ck_invites_revoke_reason
        CHECK (revoke_reason IS NULL OR revoke_reason IN ('revoked', 'expired', 'resent', 'user_deleted'))
    """,
    """
    ALTER TABLE invites ADD CONSTRAINT ck_invites_revoked_together
        CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL))
    """,
]

DROPS = [
    "ALTER TABLE invites DROP CONSTRAINT ck_invites_revoked_together",
    "ALTER TABLE invites DROP CONSTRAINT ck_invites_revoke_reason",
    "ALTER TABLE invites DROP COLUMN revoke_reason",
    "ALTER TABLE seat_state DROP CONSTRAINT ck_seat_state_status",
    "DROP INDEX ux_join_requests_pending",
    # reservations (no status yet) cannot exist in the old shape
    "DELETE FROM idempotency_keys WHERE status_code IS NULL",
    "ALTER TABLE idempotency_keys DROP CONSTRAINT ck_idempotency_keys_state",
    "ALTER TABLE idempotency_keys DROP COLUMN locked_until",
    "ALTER TABLE idempotency_keys DROP COLUMN state",
    "ALTER TABLE idempotency_keys ALTER COLUMN status_code SET NOT NULL",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROPS:
        op.execute(statement)
