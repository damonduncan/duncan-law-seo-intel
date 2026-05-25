from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import String, Integer, Text, DateTime, JSON
from sqlalchemy.orm import mapped_column, Mapped
from app.models.base import Base, new_uuid, utcnow


class ReviewSentiment(Base):
    __tablename__ = "review_sentiment"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    competitor_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    themes: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    strengths: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    weaknesses: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
