"""no-op revision: text-embedding-3-large at 1536 dims requires no schema change

Revision ID: emb3lg_upgrade
Revises: ee0a4c4e7c9d
Create Date: 2026-06-04 18:00:00.000000

The original plan was to migrate vector columns to 3072 dimensions to match
text-embedding-3-large's native size, but pgvector's HNSW index has a hard
2000-dimension limit. Instead we use text-embedding-3-large with the
`dimensions=1536` API parameter, which still outperforms text-embedding-3-small
at the same width while keeping the HNSW index functional.

Application code change only: see engine/llm/embeddings.py. This Alembic
revision exists solely to keep migration history linear after an aborted
attempt.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "emb3lg_upgrade"
down_revision: str | Sequence[str] | None = "ee0a4c4e7c9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema change. See module docstring."""


def downgrade() -> None:
    """No schema change. See module docstring."""
