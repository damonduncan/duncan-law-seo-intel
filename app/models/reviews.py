from typing import Optional, Any, Dict
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Integer, Numeric, DateTime, JSON
from sqlalchemy.orm import mapped_column, Mapped
from app.models.base import Base, new_uuid, utcnow


class ReviewSnapshot(Base):
    __tablename__ = "review_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    competitor_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # market is set for own-firm rows (one Place ID per city); None for competitors
    market: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # 'google', 'bbb'
    rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2), nullable=True)
    review_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    snapshot_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    snapped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
