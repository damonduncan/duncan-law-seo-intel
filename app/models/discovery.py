from typing import Optional, Any, Dict
from datetime import datetime
from sqlalchemy import String, DateTime, JSON
from sqlalchemy.orm import mapped_column, Mapped
from app.models.base import Base, new_uuid, utcnow


class DiscoveryCache(Base):
    __tablename__ = "discovery_cache"

    id:         Mapped[str]                    = mapped_column(String(36), primary_key=True, default=new_uuid)
    key:        Mapped[str]                    = mapped_column(String(100), nullable=False, unique=True)
    value:      Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime]               = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
