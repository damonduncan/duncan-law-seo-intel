from typing import Optional, Any, Dict
from datetime import datetime
from sqlalchemy import String, Boolean, SmallInteger, DateTime, JSON
from sqlalchemy.orm import mapped_column, Mapped
from app.models.base import Base, new_uuid, utcnow


class LocalPackRanking(Base):
    __tablename__ = "local_pack_rankings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    competitor_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    keyword: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(50), nullable=False)
    rank_position: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    in_pack: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_own_firm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
