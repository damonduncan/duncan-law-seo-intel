import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import cast, Date
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.alerts import Alert
from app.models.competitor import Competitor
from app.models.filings import FilingSnapshot
from app.models.rankings import LocalPackRanking
from app.models.reviews import ReviewSnapshot

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/competitors", response_class=HTMLResponse)
def competitors_list(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    competitors = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).order_by(Competitor.name).all()

    return templates.TemplateResponse("competitors.html", {
        "request": request,
        "user": user,
        "competitors": competitors,
        "active_page": "competitors",
    })


@router.get("/competitors/{comp_id}", response_class=HTMLResponse)
def competitor_detail(
    comp_id: str,
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    comp = db.query(Competitor).filter(Competitor.id == comp_id).first()
    if not comp:
        return RedirectResponse(url="/competitors", status_code=303)

    # ── Rankings presence ────────────────────────────────────────────────────
    latest_rank_row = (
        db.query(LocalPackRanking.scraped_at)
        .filter(LocalPackRanking.competitor_id == comp_id)
        .order_by(LocalPackRanking.scraped_at.desc())
        .first()
    )
    ranking_as_of = None
    pack_presence: dict = {}   # market → {keyword_short → rank}
    if latest_rank_row:
        ranking_as_of = latest_rank_row[0].date()
        rank_rows = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.competitor_id == comp_id,
                LocalPackRanking.in_pack == True,
                cast(LocalPackRanking.scraped_at, Date) == ranking_as_of,
            )
            .all()
        )
        for r in rank_rows:
            # Strip trailing city name from keyword for compact display
            kw = r.keyword or ""
            for suffix in [" Greensboro", " Winston-Salem", " High Point", " Charlotte",
                           " Salisbury", " Asheville", " Raleigh", " Fayetteville",
                           " Wilmington", " Wilson"]:
                if kw.endswith(suffix):
                    kw = kw[: -len(suffix)]
                    break
            pack_presence.setdefault(r.market, {})[kw] = r.rank_position

    # ── PACER filing history ─────────────────────────────────────────────────
    filing_snaps = db.query(FilingSnapshot).filter(
        FilingSnapshot.competitor_id == comp_id
    ).all()

    # De-dupe (same approach as filings router) then aggregate per (district, period)
    filing_deduped: dict = {}
    for s in filing_snaps:
        key = (s.attorney_id, s.district, s.chapter, s.period_start)
        if key not in filing_deduped or s.case_count > filing_deduped[key]:
            filing_deduped[key] = s.case_count

    pacer_raw: dict = defaultdict(lambda: defaultdict(int))
    for (_, dist, _, per), count in filing_deduped.items():
        pacer_raw[dist][per] += count

    # Sort periods ascending per district
    pacer_data: dict = {
        dist: sorted(periods.items())
        for dist, periods in pacer_raw.items()
    }

    # ── Google reviews ───────────────────────────────────────────────────────
    since = datetime.now(timezone.utc) - timedelta(days=60)
    all_review_snaps = (
        db.query(ReviewSnapshot)
        .filter(
            ReviewSnapshot.competitor_id == comp_id,
            ReviewSnapshot.source == "google",
            ReviewSnapshot.snapped_at >= since,
        )
        .order_by(ReviewSnapshot.market, ReviewSnapshot.snapped_at.desc())
        .all()
    )

    # Group by market; each market's list is newest-first
    by_market: dict = defaultdict(list)
    for s in all_review_snaps:
        by_market[s.market].append(s)

    def _dedup(snaps):
        """Deduplicate by snapshot_data fingerprint — same Google listing across market rows."""
        seen, out = set(), []
        for s in snaps:
            fp = json.dumps(s.snapshot_data, sort_keys=True) if s.snapshot_data else str(id(s))
            if fp not in seen:
                seen.add(fp)
                out.append(s)
        return out

    current_by_market = {m: snaps[0] for m, snaps in by_market.items() if snaps}
    prev_by_market = {m: snaps[1] for m, snaps in by_market.items() if len(snaps) > 1}

    current_unique = _dedup(list(current_by_market.values()))
    prev_unique = _dedup(list(prev_by_market.values()))

    total_count = sum(s.review_count for s in current_unique if s.review_count) or None
    total_prev = sum(s.review_count for s in prev_unique if s.review_count) or None
    review_delta = (total_count - total_prev) if (total_count is not None and total_prev is not None) else None

    ratings = [float(s.rating) for s in current_unique if s.rating]
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
    last_collected = max((s.snapped_at for s in current_unique), default=None)

    # Per-market breakdown for multi-location competitors
    review_by_location = []
    if len(current_by_market) > 1:
        for market in sorted(current_by_market):
            s = current_by_market[market]
            prev = prev_by_market.get(market)
            delta = None
            if prev and s.review_count is not None and prev.review_count is not None:
                delta = s.review_count - prev.review_count
            review_by_location.append({
                "market": market,
                "count": s.review_count,
                "rating": float(s.rating) if s.rating else None,
                "delta": delta,
            })

    # Build review trend chart data (per market, ascending time)
    trend_by_market: dict = {}
    for market, snaps in by_market.items():
        mdata: dict = {}
        for s in reversed(snaps):  # oldest first
            day = s.snapped_at.strftime("%Y-%m-%d")
            if s.review_count is not None:
                mdata[day] = s.review_count
        if mdata:
            trend_by_market[market] = mdata

    all_trend_dates = sorted({d for mdata in trend_by_market.values() for d in mdata})
    comp_review_chart = None
    if trend_by_market and len(all_trend_dates) >= 2:
        _colors = ["#F97316", "#2563EB", "#10B981", "#8B5CF6", "#EF4444", "#14B8A6"]
        series = []
        for i, market in enumerate(sorted(trend_by_market)):
            label = (market or "Primary").replace("_", " ").title()
            series.append({
                "label": label,
                "color": _colors[i % len(_colors)],
                "data": [trend_by_market[market].get(d) for d in all_trend_dates],
            })
        comp_review_chart = {
            "labels": [d[5:].replace("-", "/") for d in all_trend_dates],
            "series": series,
        }

    # ── Recent alerts ────────────────────────────────────────────────────────
    recent_alerts = (
        db.query(Alert)
        .filter(Alert.competitor_id == comp_id)
        .order_by(Alert.triggered_at.desc())
        .limit(10)
        .all()
    )

    return templates.TemplateResponse("competitor_detail.html", {
        "request":            request,
        "user":               user,
        "active_page":        "competitors",
        "comp":               comp,
        "ranking_as_of":      ranking_as_of,
        "pack_presence":      pack_presence,
        "pacer_data":         pacer_data,
        "total_count":        total_count,
        "total_prev":         total_prev,
        "avg_rating":         avg_rating,
        "review_delta":       review_delta,
        "last_collected":     last_collected,
        "review_by_location": review_by_location,
        "comp_review_chart":  comp_review_chart,
        "recent_alerts":      recent_alerts,
    })
