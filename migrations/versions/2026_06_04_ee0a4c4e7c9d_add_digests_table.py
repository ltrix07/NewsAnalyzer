"""add digests table

Revision ID: ee0a4c4e7c9d
Revises: c8d6c378d0f1
Create Date: 2026-06-04 14:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ee0a4c4e7c9d"
down_revision: str | Sequence[str] | None = "c8d6c378d0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    op.create_table(
        "digests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("headline", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text(), nullable=False),
        sa.Column("confidence_level", sa.String(), nullable=False),
        sa.Column("caveats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stage_version", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_digests_event_id_created_at", "digests", ["event_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_digests_created_at_desc",
        "digests",
        [sa.literal_column("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_index("ix_digests_created_at_desc", table_name="digests")
    op.drop_index("ix_digests_event_id_created_at", table_name="digests")
    op.drop_table("digests")
