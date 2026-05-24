from collections import defaultdict
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.reviews import ReviewSnapshot
from app.models.competitor import Competitor
from app.models.alerts import JobRun

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/reviews", response_class=HTMLResponse)
def reviews(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    has_data = db.query(ReviewSnapshot).first() is not None

    last_weekly = (
        db.query(JobRun)
        .filter(JobRun.job_name == "weekly")
        .order_by(JobRun.started_at.desc())
        .first()
    )
    competitor_count = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).count()

    if not has_data:
        return templates.TemplateResponse("reviews.html", {
            "request": request,
            "user": user,
            "active_page": "reviews",
            "has_data": False,
            "last_weekly": last_weekly,
            "competitor_count": competitor_count,
        })

    # Most recent snapshots — last 60 days, newest first
    since = datetime.now(timezone.utc) - timedelta(days=60)
    all_snaps = (
        db.query(ReviewSnapshot)
        .filter(ReviewSnapshot.snapped_at >= since)
        .order_by(ReviewSnapshot.snapped_at.desc())
        .all()
    )

    # Group by (competitor_id, source, market) — list ordered newest first
    snap_history: dict = defaultdict(list)
    for s in all_snaps:
        snap_history[(s.competitor_id, s.source, s.market)].append(s)

    # Build current and previous lookups: competitor_id → {source: [snaps]}
    snap_index: dict = {}
    prev_index: dict = {}
    for (cid, source, _market), snaps in snap_history.items():
        snap_index.setdefault(cid, {}).setdefault(source, []).append(snaps[0])
        if len(snaps) > 1:
            prev_index.setdefault(cid, {}).setdefault(source, []).append(snaps[1])

    # Own firm
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    own_google_snaps = sorted(
        snap_index.get(own_firm.id, {}).get("google", []) if own_firm else [],
        key=lambda s: s.market or "",
    )

    own_ratings = [float(s.rating) for s in own_google_snaps if s.rating]
    own_counts = [s.review_count for s in own_google_snaps if s.review_count]
    own_avg_rating = round(sum(own_ratings) / len(own_ratings), 1) if own_ratings else None
    own_total_reviews = sum(own_counts) if own_counts else None

    # Own firm per-market week-over-week deltas
    prev_own = prev_index.get(own_firm.id, {}).get("google", []) if own_firm else []
    prev_own_by_market = {s.market: s for s in prev_own}
    own_snap_deltas: dict = {}
    for s in own_google_snaps:
        prev = prev_own_by_market.get(s.market)
        if prev and s.review_count is not None and prev.review_count is not None:
            own_snap_deltas[s.market] = s.review_count - prev.review_count

    # Competitor rows
    competitors = (
        db.query(Competitor)
        .filter(Competitor.is_own_firm == False, Competitor.active == True)
        .order_by(Competitor.name)
        .all()
    )

    comp_rows = []
    for c in competitors:
        google_snaps = snap_index.get(c.id, {}).get("google", [])
        prev_google = prev_index.get(c.id, {}).get("google", [])

        # Sum counts and average ratings across all locations for this competitor
        counts = [s.review_count for s in google_snaps if s.review_count is not None]
        ratings = [float(s.rating) for s in google_snaps if s.rating]
        prev_counts = [s.review_count for s in prev_google if s.review_count is not None]

        total_count = sum(counts) if counts else None
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
        prev_total = sum(prev_counts) if prev_counts else None
        delta = (total_count - prev_total) if (total_count is not None and prev_total is not None) else None

        comp_rows.append({
            "name": c.name,
            "google_rating": avg_rating,
            "google_count": total_count,
            "count_delta": delta,
            "last_updated": google_snaps[0].snapped_at if google_snaps else None,
        })

    comp_rows.sort(key=lambda r: r["google_count"] or 0, reverse=True)

    # Top competitors gaining reviews this period
    velocity_leaders = sorted(
        [r for r in comp_rows if r["count_delta"] is not None and r["count_delta"] > 0],
        key=lambda r: r["count_delta"],
        reverse=True,
    )[:5]

    recommendations = _build_recommendations(own_google_snaps, comp_rows)

    return templates.TemplateResponse("reviews.html", {
        "request": request,
        "user": user,
        "active_page": "reviews",
        "has_data": True,
        "own_firm": own_firm,
        "own_google_snaps": own_google_snaps,
        "own_avg_rating": own_avg_rating,
        "own_total_reviews": own_total_reviews,
        "own_snap_deltas": own_snap_deltas,
        "comp_rows": comp_rows,
        "competitor_count": len(comp_rows),
        "recommendations": recommendations,
        "velocity_leaders": velocity_leaders,
    })


def _build_recommendations(own_snaps: list, comp_rows: list) -> list:
    recs = []
    top_comp_count = comp_rows[0]["google_count"] if comp_rows else 0

    for s in own_snaps:
        market = (s.market or "").replace("_", " ").title()
        count = s.review_count or 0

        if count < 5:
            recs.append({
                "priority": "high",
                "text": f"{market}: Only {count} review{'s' if count != 1 else ''} on this listing. "
                        f"This is well below competitors and directly limits pack visibility for neutral searchers. "
                        f"Launch a review request campaign for {market} clients immediately.",
            })
        elif count < 20:
            recs.append({
                "priority": "medium",
                "text": f"{market}: {count} reviews. Building this toward 30+ will strengthen pack ranking stability.",
            })

    # Flag largest review gap vs top competitor
    if own_snaps and top_comp_count:
        own_total = sum(s.review_count or 0 for s in own_snaps)
        if top_comp_count > own_total * 1.5:
            top_name = comp_rows[0]["name"]
            recs.append({
                "priority": "medium",
                "text": f"{top_name} leads the market with {top_comp_count:,} reviews vs. your combined "
                        f"{own_total:,}. Consistent firm-wide review requests across all 6 markets close this gap over time.",
            })

    return recs
