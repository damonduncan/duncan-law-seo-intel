"""add market column to review_snapshots

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-22

review_snapshots was created in 0001 without a market column.
This migration adds it. The table, indexes, and all other columns
already exist from the initial schema migration.

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "review_snapshots",
        sa.Column("market", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_snapshots", "market")
