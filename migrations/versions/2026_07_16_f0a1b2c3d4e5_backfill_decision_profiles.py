"""backfill historical per-user decision profiles

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4

Downgrade is intentionally a no-op because the migration cannot distinguish
rows that were historically NULL from rows attributed to the default profile
after multi-user support was introduced.
"""

from collections.abc import Sequence

from alembic import op

from engine.config import get_settings
from engine.decision_backfill import historical_profile_backfill_statement

revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Attribute legacy per-user decisions to the pre-multi-user profile."""

    op.execute(historical_profile_backfill_statement(get_settings().profile_name))


def downgrade() -> None:
    """Leave backfilled values intact because their prior state is unknowable."""
