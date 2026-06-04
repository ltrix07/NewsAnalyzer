"""enable pgvector + embeddings table

Revision ID: 1964a84cf428
Revises: 6bc24fbae224
Create Date: 2026-06-03 23:36:09.886883

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1964a84cf428"
down_revision: str | Sequence[str] | None = "6bc24fbae224"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "embeddings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("vector", pgvector.sqlalchemy.Vector(1536), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("embeddings")
    op.execute("DROP EXTENSION IF EXISTS vector")
