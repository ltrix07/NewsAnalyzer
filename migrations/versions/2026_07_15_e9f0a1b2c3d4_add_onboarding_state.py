"""add onboarding state and nullable user profile

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e9f0a1b2c3d4"
down_revision: str | Sequence[str] | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("users", "profile", existing_type=postgresql.JSONB(), nullable=True)
    op.create_table(
        "onboarding_state",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("onboarding_state")
    op.alter_column("users", "profile", existing_type=postgresql.JSONB(), nullable=False)
