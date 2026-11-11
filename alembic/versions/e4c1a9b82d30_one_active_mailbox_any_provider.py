"""one active mailbox, whichever provider

Revision ID: e4c1a9b82d30
Revises: d8e2b4c71a05
Create Date: 2026-11-11 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.services.mailboxes import ONE_ACTIVE_MAILBOX_INDEX, deactivate_extra_mailboxes


revision: str = "e4c1a9b82d30"
down_revision: Union[str, Sequence[str], None] = "d8e2b4c71a05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The Microsoft partial index still allows one Google row beside it.
    # Hide every extra active row first, then enforce one active mailbox.
    deactivate_extra_mailboxes(op.get_bind())
    op.create_index(
        ONE_ACTIVE_MAILBOX_INDEX,
        "connected_accounts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("disconnected_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(ONE_ACTIVE_MAILBOX_INDEX, table_name="connected_accounts")
