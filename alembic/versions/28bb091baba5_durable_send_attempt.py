"""Allow an approval snapshot to exist before the provider confirms the send."""

import sqlalchemy as sa
from alembic import op

revision = "28bb091baba5"
down_revision = "1eeb2a6c7994"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("provider_reference", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("send_failed_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("approvals", "approved_at", nullable=True)
    op.create_index(
        "uq_approvals_pending_message",
        "approvals",
        ["message_id"],
        unique=True,
        postgresql_where=sa.text("approved_at IS NULL AND send_failed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_approvals_pending_message", table_name="approvals")
    op.execute("DELETE FROM approvals WHERE approved_at IS NULL")
    op.alter_column("approvals", "approved_at", nullable=False)
    op.drop_column("approvals", "send_failed_at")
    op.drop_column("approvals", "provider_reference")
