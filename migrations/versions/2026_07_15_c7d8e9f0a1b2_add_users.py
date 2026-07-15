"""add users

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ui_language", sa.String(), server_default=sa.text("'ru'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint("username ~ '^[a-z0-9_]+$'", name="ck_users_username_slug"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", name="uq_users_chat_id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )


def downgrade() -> None:
    op.drop_table("users")
