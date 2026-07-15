"""add ui events

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-07-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only UI usage table."""

    op.create_table(
        "ui_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ui_events_chat_created",
        "ui_events",
        ["chat_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_ui_events_action_created",
        "ui_events",
        ["action", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    """Drop the UI usage table and its indexes."""

    op.drop_index("ix_ui_events_action_created", table_name="ui_events")
    op.drop_index("ix_ui_events_chat_created", table_name="ui_events")
    op.drop_table("ui_events")
