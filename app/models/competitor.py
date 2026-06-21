from typing import Optional, List
from sqlalchemy import String, Boolean, Text, ForeignKey
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.models.base import Base, TimestampMixin, new_uuid


class Competitor(Base, TimestampMixin):
    __tablename__ = "competitors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    config_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    google_place_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    bbb_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    domain: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    is_own_firm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    attorneys: Mapped[List["CompetitorAttorney"]] = relationship(
        back_populates="competitor", cascade="all, delete-orphan"
    )
    locations: Mapped[List["CompetitorLocation"]] = relationship(
        back_populates="competitor", cascade="all, delete-orphan"
    )

    def place_id_for_market(self, market: str) -> Optional[str]:
        """Return the Google Place ID for a specific market, falling back to the firm-level id."""
        for loc in self.locations:
            if loc.market == market and loc.google_place_id:
                return loc.google_place_id
        return self.google_place_id

    def __repr__(self) -> str:
        return f"<Competitor {self.name}>"


class CompetitorAttorney(Base, TimestampMixin):
    __tablename__ = "competitor_attorneys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    competitor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attorney_name: Mapped[str] = mapped_column(String(200), nullable=False)
    pacer_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    competitor: Mapped["Competitor"] = relationship(back_populates="attorneys")
    aliases: Mapped[List["AttorneyAlias"]] = relationship(
        back_populates="attorney", cascade="all, delete-orphan"
    )

    def all_names(self) -> List[str]:
        return [self.attorney_name] + [a.alias for a in self.aliases]

    def __repr__(self) -> str:
        return f"<CompetitorAttorney {self.attorney_name}>"


class CompetitorLocation(Base, TimestampMixin):
    __tablename__ = "competitor_locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    competitor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    market: Mapped[str] = mapped_column(String(50), nullable=False)
    google_place_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    competitor: Mapped["Competitor"] = relationship(back_populates="locations")

    def __repr__(self) -> str:
        return f"<CompetitorLocation {self.competitor_id}:{self.market}>"


class AttorneyAlias(Base):
    __tablename__ = "attorney_aliases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    attorney_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("competitor_attorneys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(200), nullable=False)

    attorney: Mapped["CompetitorAttorney"] = relationship(back_populates="aliases")
