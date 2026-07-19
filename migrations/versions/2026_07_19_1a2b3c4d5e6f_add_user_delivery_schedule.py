"""add user delivery schedule

Revision ID: 1a2b3c4d5e6f
Revises: f0a1b2c3d4e5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | Sequence[str] | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "timezone",
            sa.String(),
            server_default=sa.text("'Europe/Warsaw'"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "delivery_slot", sa.String(), server_default=sa.text("'morning'"), nullable=False
        ),
    )
    op.add_column("users", sa.Column("last_delivery_date", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_users_delivery_slot", "users", "delivery_slot IN ('morning', 'day', 'evening')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_delivery_slot", "users", type_="check")
    op.drop_column("users", "last_delivery_date")
    op.drop_column("users", "delivery_slot")
    op.drop_column("users", "timezone")
