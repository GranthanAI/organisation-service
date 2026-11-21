"""outbox.correlation_id: the relay runs long after the request, so the id is stored with the row and goes out as a
Kafka header."""
from alembic import op

revision = "0006_outbox_correlation"
down_revision = "0005_plumbing_fixes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE outbox ADD COLUMN correlation_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE outbox DROP COLUMN correlation_id")
