"""seat_state.pending_seats: a seat decrease billing has accepted but not yet applied (it takes effect at the period end).

Shown to admins in /org/v1/me as seats.pending. NULL means nothing is pending.
"""
from alembic import op

revision = "0010_pending_seats"
down_revision = "0009_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE seat_state ADD COLUMN pending_seats integer")
    op.execute("ALTER TABLE seat_state ADD CONSTRAINT ck_seat_state_pending_seats CHECK (pending_seats IS NULL OR pending_seats >= 0)")


def downgrade() -> None:
    op.execute("ALTER TABLE seat_state DROP CONSTRAINT ck_seat_state_pending_seats")
    op.execute("ALTER TABLE seat_state DROP COLUMN pending_seats")
