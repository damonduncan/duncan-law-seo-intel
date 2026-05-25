"""add review_sentiment table

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-25

Stores Claude-generated sentiment analysis of competitor Google reviews:
themes, strengths, weaknesses, and a one-sentence summary. Updated weekly.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_sentiment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("competitor_id", sa.String(36), nullable=False, index=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("themes", sa.JSON(), nullable=True),
        sa.Column("strengths", sa.JSON(), nullable=True),
        sa.Column("weaknesses", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("review_sentiment")
