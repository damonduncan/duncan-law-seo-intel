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

    # Deduplicate: (competitor_id, source, market) → most recent snapshot
    seen: dict = {}
    for s in all_snaps:
        key = (s.competitor_id, s.source, s.market)
        if key not in seen:
            seen[key] = s

    # Index: competitor_id → {source: [snapshots]}
    snap_index: dict = {}
    for (cid, source, _market), snap in seen.items():
        snap_index.setdefault(cid, {}).setdefault(source, []).append(snap)

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
        bbb_snaps = snap_index.get(c.id, {}).get("bbb", [])
        g = google_snaps[0] if google_snaps else None
        b = bbb_snaps[0] if bbb_snaps else None

        comp_rows.append({
            "name": c.name,
            "google_rating": float(g.rating) if g and g.rating else None,
            "google_count": g.review_count if g else None,
            "bbb_grade": b.snapshot_data.get("letter_grade") if b and b.snapshot_data else None,
            "bbb_complaint_count": (
                b.snapshot_data.get("complaint_count") if b and b.snapshot_data else None
            ),
            "has_bbb_url": bool(c.bbb_url),
            "last_updated": (g or b).snapped_at if (g or b) else None,
        })

    comp_rows.sort(key=lambda r: r["google_count"] or 0, reverse=True)

    return templates.TemplateResponse("reviews.html", {
        "request": request,
        "user": user,
        "active_page": "reviews",
        "has_data": True,
        "own_firm": own_firm,
        "own_google_snaps": own_google_snaps,
        "own_avg_rating": own_avg_rating,
        "own_total_reviews": own_total_reviews,
        "comp_rows": comp_rows,
        "competitor_count": len(comp_rows),
    })
