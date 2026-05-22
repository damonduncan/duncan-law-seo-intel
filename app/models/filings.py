from typing import Optional
from datetime import datetime, date
from sqlalchemy import String, Integer, SmallInteger, DateTime, Date
from sqlalchemy.orm import mapped_column, Mapped
from app.models.base import Base, new_uuid, utcnow


class FilingSnapshot(Base):
    __tablename__ = "filing_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    competitor_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    attorney_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    district: Mapped[str] = mapped_column(String(10), nullable=False)  # MDNC or WDNC
    chapter: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 7 or 13
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
