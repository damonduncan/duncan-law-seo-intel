import logging
from pathlib import Path
from typing import Any, Dict, List
import yaml
from sqlalchemy.orm import Session
from app.models.competitor import Competitor, CompetitorAttorney, AttorneyAlias, CompetitorLocation
from app.models.base import new_uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def load_yaml(filename: str) -> Dict[str, Any]:
    path = CONFIG_DIR / filename
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def sync_competitors(db: Session) -> None:
    """Sync competitors.yaml → database. Idempotent; safe to run on every startup."""
    data = load_yaml("competitors.yaml")
    now = datetime.now(timezone.utc)

    # Sync own firm
    own = data.get("own_firm", {})
    if own:
        _upsert_competitor(db, config_id="own_firm", data=own, is_own_firm=True, now=now)

    # Sync competitors
    for comp_data in data.get("competitors", []):
        comp_id = comp_data.get("id")
        if not comp_id:
            logger.warning("Competitor entry missing 'id' field — skipping")
            continue
        _upsert_competitor(db, config_id=comp_id, data=comp_data, is_own_firm=False, now=now)

    db.commit()

    # Validation warning: competitors with no Place ID anywhere (neither at the
    # Competitor level nor in any CompetitorLocation row). Multi-office firms
    # (e.g. Orcutt, Gourley, Flippin) intentionally have competitor.google_place_id=None
    # because their Place IDs live in CompetitorLocation rows — exclude those.
    competitors = (
        db.query(Competitor)
        .filter(
            Competitor.active == True,
            Competitor.google_place_id == None,
        )
        .all()
    )
    for c in competitors:
        has_location_place_id = any(
            loc.google_place_id for loc in c.locations
        )
        if not has_location_place_id:
            logger.warning(
                f"Competitor '{c.name}' (config_id={c.config_id}) has no google_place_id — "
                "review tracking and rankings matching will be skipped for this firm. "
                "Add google_place_id to competitors.yaml."
            )

    total = db.query(Competitor).filter(Competitor.active == True).count()
    own_count = db.query(Competitor).filter(Competitor.is_own_firm == True).count()
    logger.info(f"Config sync complete: {total} total firms ({own_count} own, {total - own_count} competitors)")


def _upsert_competitor(
    db: Session,
    config_id: str,
    data: Dict[str, Any],
    is_own_firm: bool,
    now: datetime,
) -> Competitor:
    competitor = db.query(Competitor).filter(Competitor.config_id == config_id).first()

    if competitor is None:
        competitor = Competitor(
            id=new_uuid(),
            config_id=config_id,
            name=data.get("name", config_id),
            google_place_id=data.get("google_place_id") or None,
            bbb_url=data.get("bbb_url") or None,
            domain=data.get("domain") or None,
            is_own_firm=is_own_firm,
            active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(competitor)
        logger.info(f"Added competitor: {competitor.name}")
    else:
        competitor.name = data.get("name", competitor.name)
        competitor.google_place_id = data.get("google_place_id") or None
        competitor.bbb_url = data.get("bbb_url") or None
        competitor.domain = data.get("domain") or None
        competitor.updated_at = now

    db.flush()

    # Sync attorneys
    existing_attorneys = {a.attorney_name: a for a in competitor.attorneys}
    yaml_attorneys: List[Dict] = data.get("attorneys", [])
    yaml_attorney_names = {a["name"] for a in yaml_attorneys}

    for attorney_data in yaml_attorneys:
        name = attorney_data["name"]
        if name in existing_attorneys:
            attorney = existing_attorneys[name]
        else:
            attorney = CompetitorAttorney(
                id=new_uuid(),
                competitor_id=competitor.id,
                attorney_name=name,
                pacer_id=attorney_data.get("pacer_id"),
                created_at=now,
                updated_at=now,
            )
            db.add(attorney)
            logger.info(f"  Added attorney: {name} → {competitor.name}")
            db.flush()

        # Sync aliases
        existing_aliases = {a.alias for a in attorney.aliases}
        yaml_aliases: List[str] = attorney_data.get("aliases", [])
        for alias in yaml_aliases:
            if alias not in existing_aliases:
                db.add(AttorneyAlias(
                    id=new_uuid(),
                    attorney_id=attorney.id,
                    alias=alias,
                ))

    # Sync locations (own firm has per-market entries; competitors use google_place_id per market)
    yaml_locations: List[Dict] = data.get("locations", [])
    if not yaml_locations and data.get("markets"):
        # Create one location per listed market. google_place_id may be empty for
        # PACER-only firms (e.g. EDNC competitors without a verified Google listing).
        markets_list: List[str] = data.get("markets", [])
        yaml_locations = [
            {"market": m, "google_place_id": data.get("google_place_id") or None}
            for m in markets_list
        ]

    existing_locations = {loc.market: loc for loc in competitor.locations}
    for loc_data in yaml_locations:
        market = loc_data.get("market", "")
        place_id = loc_data.get("google_place_id") or None
        if market in existing_locations:
            existing_locations[market].google_place_id = place_id
            existing_locations[market].updated_at = now
        else:
            db.add(CompetitorLocation(
                id=new_uuid(),
                competitor_id=competitor.id,
                market=market,
                google_place_id=place_id,
                created_at=now,
                updated_at=now,
            ))

    return competitor


def get_keywords() -> List[str]:
    """Return all expanded keyword strings (e.g. 'bankruptcy attorney Greensboro')."""
    data = load_yaml("keywords.yaml")
    templates: List[str] = data.get("templates", [])
    cities: List[str] = data.get("cities", [])
    return [t.format(city=city) for t in templates for city in cities]


def get_markets() -> Dict[str, Any]:
    data = load_yaml("competitors.yaml")
    return data.get("markets", {})
