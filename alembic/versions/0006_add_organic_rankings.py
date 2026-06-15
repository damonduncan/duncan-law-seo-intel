"""add organic_rankings table

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-14

Tracks Duncan Law's Google organic search positions (results below the local
3-pack) and a landscape snapshot of who else ranks organically. Updated daily
for own-firm position; weekly for competitor landscape.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organic_rankings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("keyword", sa.String(200), nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("market", sa.String(50), nullable=False),
        sa.Column("domain", sa.String(200), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("title", sa.String(400), nullable=True),
        sa.Column("rank_position", sa.SmallInteger(), nullable=True),
        sa.Column("is_own_firm", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("organic_rankings")
