"""add digest feedback dislike reason

Revision ID: c1d2e3f4a5b6
Revises: b7c8d9e0f1a2
Create Date: 2026-06-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    op.add_column("digest_feedback", sa.Column("reason", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_digest_feedback_reason",
        "digest_feedback",
        "reason is null or reason in ('off_topic','weak_analysis')",
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_constraint("ck_digest_feedback_reason", "digest_feedback", type_="check")
    op.drop_column("digest_feedback", "reason")
