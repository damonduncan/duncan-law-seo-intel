"""add review_snapshots table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-22

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("competitor_id", sa.String(36), nullable=False),
        sa.Column("market", sa.String(50), nullable=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("rating", sa.Numeric(3, 2), nullable=True),
        sa.Column("review_count", sa.Integer, nullable=True),
        sa.Column("snapshot_data", sa.JSON, nullable=True),
        sa.Column("snapped_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        if_not_exists=True,
    )
    op.create_index("ix_review_snapshots_competitor_id", "review_snapshots", ["competitor_id"], if_not_exists=True)
    op.create_index("ix_review_snapshots_snapped_at", "review_snapshots", ["snapped_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_review_snapshots_snapped_at", table_name="review_snapshots")
    op.drop_index("ix_review_snapshots_competitor_id", table_name="review_snapshots")
    op.drop_table("review_snapshots")
