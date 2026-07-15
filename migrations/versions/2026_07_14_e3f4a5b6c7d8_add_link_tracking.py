"""add link tracking

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create tracked-link and append-only click tables."""

    op.create_table(
        "digest_links",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("digest_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("citation_index", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
        sa.UniqueConstraint("digest_id", "chat_id", "citation_index"),
    )
    op.create_table(
        "link_clicks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("link_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "clicked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["link_id"], ["digest_links.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_link_clicks_link_id_clicked_at",
        "link_clicks",
        ["link_id", sa.text("clicked_at DESC")],
    )


def downgrade() -> None:
    """Drop click tracking in dependency order."""

    op.drop_index("ix_link_clicks_link_id_clicked_at", table_name="link_clicks")
    op.drop_table("link_clicks")
    op.drop_table("digest_links")
