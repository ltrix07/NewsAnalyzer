"""add events and event_members

Revision ID: c8d6c378d0f1
Revises: 1964a84cf428
Create Date: 2026-06-04 12:45:00.000000

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d6c378d0f1"
down_revision: str | Sequence[str] | None = "1964a84cf428"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("centroid", pgvector.sqlalchemy.Vector(1536), nullable=False),
        sa.Column("article_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'open'"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_last_seen_at", "events", ["last_seen_at"], unique=False)
    op.execute(
        "CREATE INDEX ix_events_centroid_hnsw ON events USING hnsw (centroid vector_cosine_ops)"
    )

    op.create_table(
        "event_members",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("similarity_to_centroid", sa.Float(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id"),
    )
    op.create_index("ix_event_members_event_id", "event_members", ["event_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_events_centroid_hnsw")
    op.drop_index("ix_event_members_event_id", table_name="event_members")
    op.drop_table("event_members")
    op.drop_index("ix_events_last_seen_at", table_name="events")
    op.drop_table("events")
