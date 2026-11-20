"""Keep the Stripe customer binding available to account deletion jobs."""

import sqlalchemy as sa
from alembic import op

revision = "a04fe6d31f0c"
down_revision = "58dac8ba4f77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(255)))
    op.create_unique_constraint("uq_users_stripe_customer_id", "users", ["stripe_customer_id"])
    op.add_column("account_deletions", sa.Column("stripe_customer_id", sa.String(255)))
    op.add_column("account_deletions", sa.Column("stripe_deleted_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("account_deletions", "stripe_deleted_at")
    op.drop_column("account_deletions", "stripe_customer_id")
    op.drop_constraint("uq_users_stripe_customer_id", "users", type_="unique")
    op.drop_column("users", "stripe_customer_id")
