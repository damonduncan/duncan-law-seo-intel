from typing import Optional, Any, Dict
from datetime import datetime
from sqlalchemy import String, DateTime, JSON, Text
from sqlalchemy.orm import mapped_column, Mapped
from app.models.base import Base, new_uuid, utcnow


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # alert_type values: 'pack_drop', 'competitor_pack_entry', 'pacer_volume_spike'
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    # severity values: 'immediate', 'weekly_digest'
    competitor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    keyword: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    market: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    detail: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    emailed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DigestLog(Base):
    __tablename__ = "digest_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # 'sent', 'failed'
    resend_message_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    # status values: 'running', 'success', 'failed'
    records_processed: Mapped[Optional[int]] = mapped_column(nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
