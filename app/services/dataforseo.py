import logging
import time
from base64 import b64encode
from typing import Any, Dict, List, Optional
import requests
from app.config import settings

logger = logging.getLogger(__name__)

DATAFORSEO_BASE = "https://api.dataforseo.com/v3"

# Maps display city name (from keywords.yaml) → DataForSEO location_name
CITY_TO_LOCATION = {
    "Greensboro":    "Greensboro,North Carolina,United States",
    "Winston-Salem": "Winston-Salem,North Carolina,United States",
    "High Point":    "High Point,North Carolina,United States",
    "Charlotte":     "Charlotte,North Carolina,United States",
    "Salisbury":     "Salisbury,North Carolina,United States",
    "Asheville":     "Asheville,North Carolina,United States",
    # EDNC markets
    "Raleigh":       "Raleigh,North Carolina,United States",
    "Fayetteville":  "Fayetteville,North Carolina,United States",
    "Wilmington":    "Wilmington,North Carolina,United States",
    "Wilson":        "Wilson,North Carolina,United States",
}

# Maps display city name → market key (matches competitors.yaml)
CITY_TO_MARKET = {
    "Greensboro":    "greensboro",
    "Winston-Salem": "winston_salem",
    "High Point":    "high_point",
    "Charlotte":     "charlotte",
    "Salisbury":     "salisbury",
    "Asheville":     "asheville",
    # EDNC markets
    "Raleigh":       "raleigh",
    "Fayetteville":  "fayetteville",
    "Wilmington":    "wilmington",
    "Wilson":        "wilson",
}


def _auth_header() -> Dict[str, str]:
    token = b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def fetch_local_pack(keyword: str, city: str) -> List[Dict[str, Any]]:
    """
    Fetch Google local pack results for a keyword/city combo.
    Returns a list of up to 3 result dicts with keys:
      rank_position, title, place_id, rating, rating_count, address
    """
    location_name = CITY_TO_LOCATION.get(city)
    if not location_name:
        logger.warning(f"No DataForSEO location mapping for city: {city}")
        return []

    payload = [{
        "keyword": keyword,
        "location_name": location_name,
        "language_name": "English",
        "device": "desktop",
        "os": "windows",
    }]

    try:
        response = requests.post(
            f"{DATAFORSEO_BASE}/serp/google/maps/live/advanced",
            headers=_auth_header(),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"DataForSEO API error for '{keyword}' in {city}: {e}")
        return []

    results = []
    try:
        tasks = data.get("tasks", [])
        if not tasks:
            return []
        task = tasks[0]
        if task.get("status_code") != 20000:
            logger.warning(
                f"DataForSEO task error for '{keyword}' in {city}: "
                f"{task.get('status_message')}"
            )
            return []

        items = (
            task.get("result", [{}])[0]
            .get("items", [])
        ) if task.get("result") else []

        for item in items:
            item_type = item.get("type", "")
            # Google Maps endpoint returns "maps_search" items for business listings
            if item_type not in ("maps_search", "local_pack"):
                continue
            place_id = (
                item.get("place_id")
                or item.get("cid", "")
            )
            results.append({
                "rank_position": item.get("rank_group") or item.get("rank_absolute"),
                "title": item.get("title", ""),
                "place_id": place_id,
                "rating": item.get("rating", {}).get("value") if isinstance(item.get("rating"), dict) else None,
                "rating_count": item.get("rating", {}).get("votes_count") if isinstance(item.get("rating"), dict) else None,
                "address": item.get("address", ""),
                "raw": item,
            })
    except Exception as e:
        logger.error(f"Failed to parse DataForSEO response for '{keyword}' in {city}: {e}")

    return results


def collect_rankings_for_keywords(
    keywords: List[str],
    own_place_ids: List[str],
    competitor_place_map: Dict[str, str],
    db,
    own_firm_id: str,
    only_own_firm: bool = False,
    delay_seconds: float = 0.5,
) -> int:
    """
    Fetch local pack results for a list of expanded keyword strings
    (e.g. "bankruptcy attorney Greensboro") and store in DB.

    own_place_ids: list of own firm's place IDs across all markets
    competitor_place_map: {place_id: competitor_id}
    only_own_firm: if True, only store rows for own firm (daily job)
    Returns count of rows stored.
    """
    from datetime import datetime, timezone, date
    from app.models.rankings import LocalPackRanking
    from app.models.base import new_uuid

    rows_stored = 0
    today = date.today()

    for keyword_city in keywords:
        # Parse "bankruptcy attorney Greensboro" → city is last word(s)
        city = _extract_city(keyword_city)
        if not city:
            continue

        market = CITY_TO_MARKET.get(city, "")
        results = fetch_local_pack(keyword_city, city)

        # Build set of place_ids in today's pack
        pack_place_ids = {r["place_id"] for r in results}

        # Determine which competitor IDs to store
        ids_to_store = set()

        # Always store own firm entries
        for r in results:
            if r["place_id"] in own_place_ids:
                ids_to_store.add("own")

        if not only_own_firm:
            for r in results:
                if r["place_id"] in competitor_place_map:
                    ids_to_store.add(competitor_place_map[r["place_id"]])

        # Store own firm result (or absence)
        # Only the top 3 results constitute the actual Google local 3-pack
        pack_results = [r for r in results if r["rank_position"] and r["rank_position"] <= 3]

        own_result = next(
            (r for r in results if r["place_id"] in own_place_ids), None
        )
        own_in_pack = own_result is not None and own_result.get("rank_position", 99) <= 3

        _upsert_ranking(
            db=db,
            competitor_id=own_firm_id,
            keyword=keyword_city,
            city=city,
            market=market,
            rank_position=own_result["rank_position"] if own_result else None,
            in_pack=own_in_pack,
            is_own_firm=True,
            result_data=own_result,
            today=today,
        )
        rows_stored += 1

        if not only_own_firm:
            for r in pack_results:
                comp_id = competitor_place_map.get(r["place_id"])
                if comp_id:
                    _upsert_ranking(
                        db=db,
                        competitor_id=comp_id,
                        keyword=keyword_city,
                        city=city,
                        market=market,
                        rank_position=r["rank_position"],
                        in_pack=True,
                        is_own_firm=False,
                        result_data=r,
                        today=today,
                    )
                    rows_stored += 1

        db.commit()
        time.sleep(delay_seconds)

    return rows_stored


def _upsert_ranking(
    db, competitor_id, keyword, city, market,
    rank_position, in_pack, is_own_firm, result_data, today
):
    from datetime import datetime, timezone
    from app.models.rankings import LocalPackRanking
    from app.models.base import new_uuid
    from sqlalchemy import func, cast
    from sqlalchemy.types import Date

    # One row per competitor + keyword + date
    existing = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == competitor_id,
            LocalPackRanking.keyword == keyword,
            LocalPackRanking.city == city,
            cast(LocalPackRanking.scraped_at, Date) == today,
        )
        .first()
    )

    now = datetime.now(timezone.utc)
    if existing:
        existing.rank_position = rank_position
        existing.in_pack = in_pack
        existing.result_data = result_data
        existing.scraped_at = now
    else:
        db.add(LocalPackRanking(
            id=new_uuid(),
            competitor_id=competitor_id,
            keyword=keyword,
            city=city,
            market=market,
            rank_position=rank_position,
            in_pack=in_pack,
            is_own_firm=is_own_firm,
            result_data=result_data,
            scraped_at=now,
        ))


def _extract_city(keyword_city: str) -> Optional[str]:
    """Extract city from an expanded keyword like 'bankruptcy attorney Greensboro'."""
    for city in CITY_TO_LOCATION.keys():
        if keyword_city.endswith(city):
            return city
    return None


def build_place_maps(db):
    """
    Returns:
      own_firm_id: str
      own_place_ids: List[str]  — all place IDs across own firm's 6 locations
      competitor_place_map: Dict[place_id, competitor_id]
    """
    from app.models.competitor import Competitor, CompetitorLocation

    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    own_firm_id = own_firm.id if own_firm else None

    own_place_ids = []
    if own_firm:
        own_place_ids = [
            loc.google_place_id
            for loc in own_firm.locations
            if loc.google_place_id
        ]

    competitors = db.query(Competitor).filter(
        Competitor.is_own_firm == False,
        Competitor.active == True,
    ).all()

    competitor_place_map = {}
    for comp in competitors:
        if comp.google_place_id:
            competitor_place_map[comp.google_place_id] = comp.id
        for loc in comp.locations:
            if loc.google_place_id:
                competitor_place_map[loc.google_place_id] = comp.id

    return own_firm_id, own_place_ids, competitor_place_map
