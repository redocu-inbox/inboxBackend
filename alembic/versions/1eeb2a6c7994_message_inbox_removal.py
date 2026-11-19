"""Store the time a message left the inbox through an app action."""

import sqlalchemy as sa
from alembic import op

revision = "1eeb2a6c7994"
down_revision = "d4f8a1c63e20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("inbox_removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "inbox_removed_at")
