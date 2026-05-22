"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-21

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "competitors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("config_id", sa.String(50), unique=True, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("google_place_id", sa.String(100), nullable=True),
        sa.Column("bbb_url", sa.String(500), nullable=True),
        sa.Column("domain", sa.String(200), nullable=True),
        sa.Column("is_own_firm", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "competitor_attorneys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("competitor_id", sa.String(36), nullable=False, index=True),
        sa.Column("attorney_name", sa.String(200), nullable=False),
        sa.Column("pacer_id", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "attorney_aliases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("attorney_id", sa.String(36), nullable=False, index=True),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(["attorney_id"], ["competitor_attorneys.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(200), unique=True, nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("google_sub", sa.String(200), unique=True, nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "local_pack_rankings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("competitor_id", sa.String(36), nullable=True, index=True),
        sa.Column("keyword", sa.String(200), nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("market", sa.String(50), nullable=False),
        sa.Column("rank_position", sa.SmallInteger(), nullable=True),
        sa.Column("in_pack", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_own_firm", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("result_data", sa.JSON(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )

    op.create_table(
        "review_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("competitor_id", sa.String(36), nullable=False, index=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("rating", sa.Numeric(3, 2), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("snapshot_data", sa.JSON(), nullable=True),
        sa.Column("snapped_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )

    op.create_table(
        "filing_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("competitor_id", sa.String(36), nullable=False, index=True),
        sa.Column("attorney_id", sa.String(36), nullable=True),
        sa.Column("district", sa.String(10), nullable=False),
        sa.Column("chapter", sa.SmallInteger(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapped_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("alert_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("competitor_id", sa.String(36), nullable=True),
        sa.Column("keyword", sa.String(200), nullable=True),
        sa.Column("market", sa.String(50), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "digest_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recipient", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("resend_message_id", sa.String(200), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
    )

    op.create_table(
        "job_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_name", sa.String(100), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("records_processed", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("job_runs")
    op.drop_table("digest_log")
    op.drop_table("alerts")
    op.drop_table("filing_snapshots")
    op.drop_table("review_snapshots")
    op.drop_table("local_pack_rankings")
    op.drop_table("attorney_aliases")
    op.drop_table("competitor_attorneys")
    op.drop_table("competitors")
    op.drop_table("users")
