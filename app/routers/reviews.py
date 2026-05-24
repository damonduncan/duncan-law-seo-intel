import json
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.reviews import ReviewSnapshot
from app.models.competitor import Competitor, CompetitorLocation
from app.models.alerts import JobRun

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()

MARKET_TO_DISTRICT = {
    "greensboro": "MDNC", "winston_salem": "MDNC", "high_point": "MDNC",
    "salisbury": "MDNC", "durham": "MDNC", "concord": "MDNC",
    "graham": "MDNC", "carthage": "MDNC", "asheboro": "MDNC",
    "charlotte": "WDNC", "asheville": "WDNC", "waynesville": "WDNC",
    "statesville": "WDNC", "mooresville": "WDNC", "elkin": "WDNC",
    "north_wilkesboro": "WDNC", "morganton": "WDNC",
    "ednc": "EDNC", "raleigh": "EDNC", "fayetteville": "EDNC",
    "wilson": "EDNC", "wilmington": "EDNC",
}
OWN_MDNC_ORDER = ["greensboro", "winston_salem", "high_point", "salisbury"]
OWN_WDNC_ORDER = ["charlotte", "asheville"]
OWN_MDNC = frozenset(OWN_MDNC_ORDER)
OWN_WDNC = frozenset(OWN_WDNC_ORDER)


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
        raw_snaps = snap_index.get(c.id, {}).get("google", [])
        raw_prev = prev_index.get(c.id, {}).get("google", [])

        # Deduplicate: same Google listing may appear under multiple market rows.
        # Use snapshot_data as a fingerprint — identical API responses == same listing.
        def _dedup(snaps):
            seen, out = set(), []
            for s in snaps:
                fp = json.dumps(s.snapshot_data, sort_keys=True) if s.snapshot_data else id(s)
                if fp not in seen:
                    seen.add(fp)
                    out.append(s)
            return out

        google_snaps = _dedup(raw_snaps)
        prev_google = _dedup(raw_prev)

        # Sum counts and average ratings across all locations for this competitor
        counts = [s.review_count for s in google_snaps if s.review_count is not None]
        ratings = [float(s.rating) for s in google_snaps if s.rating]
        prev_counts = [s.review_count for s in prev_google if s.review_count is not None]

        total_count = sum(counts) if counts else None
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
        prev_total = sum(prev_counts) if prev_counts else None
        delta = (total_count - prev_total) if (total_count is not None and prev_total is not None) else None

        comp_rows.append({
            "id": c.id,
            "name": c.name,
            "google_rating": avg_rating,
            "google_count": total_count,
            "count_delta": delta,
            "last_updated": google_snaps[0].snapped_at if google_snaps else None,
        })

    comp_rows.sort(key=lambda r: r["google_count"] or 0, reverse=True)

    # Determine primary district for each competitor from their location markets
    all_locs = db.query(CompetitorLocation).all()
    comp_markets: dict = defaultdict(set)
    for loc in all_locs:
        comp_markets[loc.competitor_id].add(loc.market)

    for row in comp_rows:
        markets = comp_markets.get(row["id"], set())
        counts = Counter(MARKET_TO_DISTRICT[m] for m in markets if m in MARKET_TO_DISTRICT)
        row["district"] = counts.most_common(1)[0][0] if counts else "MDNC"

    mdnc_comps = [r for r in comp_rows if r["district"] == "MDNC"]
    wdnc_comps = [r for r in comp_rows if r["district"] == "WDNC"]
    ednc_comps = [r for r in comp_rows if r["district"] == "EDNC"]

    own_mdnc_snaps = sorted(
        [s for s in own_google_snaps if s.market in OWN_MDNC],
        key=lambda s: OWN_MDNC_ORDER.index(s.market) if s.market in OWN_MDNC else 99,
    )
    own_wdnc_snaps = sorted(
        [s for s in own_google_snaps if s.market in OWN_WDNC],
        key=lambda s: OWN_WDNC_ORDER.index(s.market) if s.market in OWN_WDNC else 99,
    )

    # Build review trend for own firm — one data point per (market, date)
    _MARKET_COLORS = {
        "greensboro": "#2563EB", "winston_salem": "#10B981", "high_point": "#F97316",
        "charlotte": "#8B5CF6", "salisbury": "#EF4444", "asheville": "#14B8A6",
    }
    _MARKET_DISPLAY = {
        "greensboro": "Greensboro", "winston_salem": "Winston-Salem",
        "high_point": "High Point", "charlotte": "Charlotte",
        "salisbury": "Salisbury", "asheville": "Asheville",
    }
    own_trend_raw: dict = defaultdict(dict)  # market → {date_str: count}
    for (cid, source, market), snaps in snap_history.items():
        if own_firm and cid == own_firm.id and source == "google" and market:
            for s in snaps:
                if s.review_count is not None:
                    own_trend_raw[market][s.snapped_at.strftime("%Y-%m-%d")] = s.review_count

    review_chart_data = None
    all_trend_dates = sorted({d for mdata in own_trend_raw.values() for d in mdata})
    if own_trend_raw and len(all_trend_dates) >= 2:
        series = []
        for market in OWN_MDNC_ORDER + OWN_WDNC_ORDER:
            if market not in own_trend_raw:
                continue
            series.append({
                "label": _MARKET_DISPLAY.get(market, market),
                "color": _MARKET_COLORS.get(market, "#94A3B8"),
                "data": [own_trend_raw[market].get(d) for d in all_trend_dates],
            })
        if series:
            review_chart_data = {
                "labels": [d[5:].replace("-", "/") for d in all_trend_dates],
                "series": series,
            }

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
        "review_chart_data": review_chart_data,
        # District-grouped data
        "mdnc_comps": mdnc_comps,
        "wdnc_comps": wdnc_comps,
        "ednc_comps": ednc_comps,
        "own_mdnc_snaps": own_mdnc_snaps,
        "own_wdnc_snaps": own_wdnc_snaps,
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
