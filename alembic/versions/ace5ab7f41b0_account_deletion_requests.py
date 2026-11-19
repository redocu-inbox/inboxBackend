"""Store account deletion requests independently of user rows."""

import sqlalchemy as sa
from alembic import op

revision = "ace5ab7f41b0"
down_revision = "61c312b77120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_deletions",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("account_deletions")
