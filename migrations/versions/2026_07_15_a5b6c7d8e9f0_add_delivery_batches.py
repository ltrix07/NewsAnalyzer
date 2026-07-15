"""add delivery batches

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_batches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("notification_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_nudge_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delivery_batches_chat_closed", "delivery_batches", ["chat_id", "closed_at"])
    op.add_column("digests", sa.Column("batch_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_digests_batch_id_delivery_batches", "digests", "delivery_batches", ["batch_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_digests_batch_id_delivery_batches", "digests", type_="foreignkey")
    op.drop_column("digests", "batch_id")
    op.drop_index("ix_delivery_batches_chat_closed", table_name="delivery_batches")
    op.drop_table("delivery_batches")
