"""add research pending table

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-06-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    op.create_table(
        "research_pending",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_table("research_pending")
