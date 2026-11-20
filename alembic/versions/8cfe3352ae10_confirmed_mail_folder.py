"""Keep confirmed provider folder state and the last reconciliation check."""

import sqlalchemy as sa
from alembic import op

revision = "8cfe3352ae10"
down_revision = "a04fe6d31f0c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("mail_folder", sa.String(16), nullable=True))
    op.add_column("messages", sa.Column("folder_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("previous_provider_id", sa.String(512), nullable=True))
    op.create_index("ix_messages_previous_provider", "messages", ["connected_account_id", "previous_provider_id"],
                    postgresql_where=sa.text("previous_provider_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_messages_previous_provider", table_name="messages")
    op.drop_column("messages", "previous_provider_id")
    op.drop_column("messages", "folder_checked_at")
    op.drop_column("messages", "mail_folder")
