"""add telegram feedback impressions listener tables

Revision ID: a1b2c3d4e5f6
Revises: ee0a4c4e7c9d
Create Date: 2026-06-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "emb3lg_upgrade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    op.create_table(
        "digest_feedback",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("feedback", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("feedback in ('like', 'dislike')", name="ck_digest_feedback_feedback"),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_digest_feedback_digest_chat_created",
        "digest_feedback",
        ["digest_id", "chat_id", sa.literal_column("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "impressions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "shown_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "discussion_pending",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )

    op.create_table(
        "telegram_cursor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_telegram_cursor_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_table("telegram_cursor")
    op.drop_table("discussion_pending")
    op.drop_table("impressions")
    op.drop_index("ix_digest_feedback_digest_chat_created", table_name="digest_feedback")
    op.drop_table("digest_feedback")
