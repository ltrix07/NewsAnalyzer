"""add source metadata

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b6c7d8e9f0a1"
down_revision: str | Sequence[str] | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("topics", postgresql.JSONB(), nullable=True))
    op.add_column("sources", sa.Column("lang", sa.String(length=8), nullable=True))
    op.add_column("sources", sa.Column("country", sa.String(length=2), nullable=True))
    op.create_index("ix_sources_topics_gin", "sources", ["topics"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_sources_topics_gin", table_name="sources", postgresql_using="gin")
    op.drop_column("sources", "country")
    op.drop_column("sources", "lang")
    op.drop_column("sources", "topics")
    op.drop_column("sources", "description")
