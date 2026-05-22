from typing import Optional
from datetime import datetime
from sqlalchemy import String, DateTime
from sqlalchemy.orm import mapped_column, Mapped
from app.models.base import Base, new_uuid, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    google_sub: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    last_login: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"
