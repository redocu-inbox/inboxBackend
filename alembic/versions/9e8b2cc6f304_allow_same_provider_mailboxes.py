"""Allow distinct Gmail and Outlook mailboxes under the same user."""

import sqlalchemy as sa
from alembic import op

from app.services.mailboxes import ACTIVE_GOOGLE_MAILBOX_INDEX, ACTIVE_MAILBOX_INDEX


revision = "9e8b2cc6f304"
down_revision = "8cfe3352ae10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(ACTIVE_MAILBOX_INDEX, table_name="connected_accounts")
    op.drop_index(ACTIVE_GOOGLE_MAILBOX_INDEX, table_name="connected_accounts")


def downgrade() -> None:
    # PostgreSQL rejects the downgrade if active accounts still violate the old limits.
    for index, provider in ((ACTIVE_MAILBOX_INDEX, "microsoft"), (ACTIVE_GOOGLE_MAILBOX_INDEX, "google")):
        op.create_index(
            index,
            "connected_accounts",
            ["user_id"],
            unique=True,
            postgresql_where=sa.text(f"disconnected_at IS NULL AND provider = '{provider}'"),
        )
