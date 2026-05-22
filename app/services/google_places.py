"""Google Places Details API client — Phase 3.

Uses the standard Places Details endpoint (maps.googleapis.com) which works
with a plain "Places API" key from Google Cloud Console — no "New" API needed.

Fetches rating and review count for every tracked firm.
Own firm: iterates per-market Place IDs (one listing per city).
Competitors: uses the single google_place_id on the Competitor record.
"""
import logging
import time
from decimal import Decimal
from typing import Optional

import requests
from sqlalchemy.orm import Session

from app.config import settings
from app.models.base import new_uuid
from app.models.competitor import Competitor, CompetitorLocation
from app.models.reviews import ReviewSnapshot

logger = logging.getLogger(__name__)

PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
# name + rating + user_ratings_total are "Basic" fields — cheapest tier ($0.017/call)
FIELDS = "name,rating,user_ratings_total"
REQUEST_DELAY = 0.25  # seconds between API calls


def collect_competitor_reviews(db: Session) -> int:
    """Collect Google review snapshots for all firms. Returns number of rows saved."""
    if not settings.google_places_api_key:
        logger.warning("GOOGLE_PLACES_API_KEY not set — skipping Google review collection")
        return 0

    records = 0

    # Own firm: one snapshot per market location
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    if own_firm:
        locations = (
            db.query(CompetitorLocation)
            .filter(CompetitorLocation.competitor_id == own_firm.id)
            .all()
        )
        for loc in locations:
            if not loc.google_place_id:
                continue
            data = _fetch_place(loc.google_place_id)
            if data:
                db.add(_make_snapshot(own_firm.id, "google", data, market=loc.market))
                records += 1
            time.sleep(REQUEST_DELAY)

    # Competitors: one snapshot per firm (primary Place ID)
    comps = (
        db.query(Competitor)
        .filter(
            Competitor.is_own_firm == False,
            Competitor.active == True,
            Competitor.google_place_id != None,
            Competitor.google_place_id != "",
        )
        .all()
    )
    for comp in comps:
        data = _fetch_place(comp.google_place_id)
        if data:
            db.add(_make_snapshot(comp.id, "google", data, market=None))
            records += 1
        time.sleep(REQUEST_DELAY)

    db.commit()
    logger.info(f"Google Places: saved {records} review snapshots")
    return records


def _fetch_place(place_id: str) -> Optional[dict]:
    """Call Places Details API for a single Place ID. Returns the result dict or None."""
    params = {
        "place_id": place_id,
        "fields": FIELDS,
        "key": settings.google_places_api_key,
    }
    try:
        resp = requests.get(PLACES_DETAILS_URL, params=params, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        status = body.get("status")
        if status == "OK":
            return body.get("result", {})
        if status == "NOT_FOUND":
            logger.warning(f"Places API: place_id not found: {place_id}")
            return None
        logger.error(f"Places API unexpected status '{status}' for {place_id}")
        return None
    except Exception as e:
        logger.error(f"Places API error for {place_id}: {e}")
        return None


def _make_snapshot(
    competitor_id: str, source: str, data: dict, market: Optional[str]
) -> ReviewSnapshot:
    # Standard Places Details response uses 'rating' and 'user_ratings_total'
    rating_val = data.get("rating")
    count_val = data.get("user_ratings_total")
    return ReviewSnapshot(
        id=new_uuid(),
        competitor_id=competitor_id,
        market=market,
        source=source,
        rating=Decimal(str(rating_val)) if rating_val is not None else None,
        review_count=int(count_val) if count_val is not None else None,
        snapshot_data=data,
    )
