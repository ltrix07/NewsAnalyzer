"""add decision profile name

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("profile_name", sa.String(), nullable=True))
    op.create_index(
        "ix_decisions_stage_target_profile",
        "decisions",
        ["stage_name", "target_type", "target_id", "profile_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_decisions_stage_target_profile", table_name="decisions")
    op.drop_column("decisions", "profile_name")
