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
from app.constants import MARKET_TO_DISTRICT
from app.utils import dedup_review_snaps

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()
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

    # Own firm per-market rolling weekly rates (4-week average)
    own_snap_deltas: dict = {}
    for s in own_google_snaps:
        if s.market:
            history = snap_history[(own_firm.id, "google", s.market)]
            own_snap_deltas[s.market] = _rolling_weekly_rate(history)

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

        google_snaps = dedup_review_snaps(raw_snaps)
        prev_google = dedup_review_snaps(raw_prev)

        # Sum counts and average ratings across all locations for this competitor
        counts = [s.review_count for s in google_snaps if s.review_count is not None]
        ratings = [float(s.rating) for s in google_snaps if s.rating]
        prev_counts = [s.review_count for s in prev_google if s.review_count is not None]

        total_count = sum(counts) if counts else None
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
        prev_total = sum(prev_counts) if prev_counts else None
        delta = (total_count - prev_total) if (total_count is not None and prev_total is not None) else None

        weekly_rate = round(sum(
            _rolling_weekly_rate(snap_history[(c.id, "google", s.market)])
            for s in google_snaps if s.market
        ), 1)

        comp_rows.append({
            "id": c.id,
            "name": c.name,
            "google_rating": avg_rating,
            "google_count": total_count,
            "count_delta": delta,
            "weekly_rate": weekly_rate,
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

    velocity_projections = _build_velocity_projections(own_google_snaps, own_snap_deltas, comp_rows)

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

    # Recent client review snippets from Google Places snapshot_data
    own_review_snippets: list = []
    for s in own_google_snaps:
        sd = s.snapshot_data or {}
        raw_reviews = sd.get("reviews", [])
        if not raw_reviews:
            continue
        mkt_display = _MARKET_DISPLAY.get(s.market, (s.market or "").replace("_", " ").title())
        for rv in raw_reviews[:3]:
            text = (rv.get("text") or "").strip()
            if not text:
                continue
            own_review_snippets.append({
                "market":        s.market,
                "display":       mkt_display,
                "rating":        rv.get("rating"),
                "text":          text[:300],
                "author":        rv.get("author_name", ""),
                "relative_time": rv.get("relative_time_description", ""),
            })
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
        "velocity_projections": velocity_projections,
        "review_chart_data": review_chart_data,
        "own_review_snippets": own_review_snippets,
        # District-grouped data
        "mdnc_comps": mdnc_comps,
        "wdnc_comps": wdnc_comps,
        "ednc_comps": ednc_comps,
        "own_mdnc_snaps": own_mdnc_snaps,
        "own_wdnc_snaps": own_wdnc_snaps,
    })


_OWN_MARKETS_META = [
    ("greensboro",    "Greensboro",    "MDNC"),
    ("winston_salem", "Winston-Salem", "MDNC"),
    ("high_point",    "High Point",    "MDNC"),
    ("salisbury",     "Salisbury",     "MDNC"),
    ("charlotte",     "Charlotte",     "WDNC"),
    ("asheville",     "Asheville",     "WDNC"),
]


def _rolling_weekly_rate(snaps: list, n_weeks: int = 4) -> float:
    """Average weekly review gain over the last n_weeks of history (snaps sorted newest-first)."""
    valid = [s for s in snaps if s.review_count is not None]
    if len(valid) < 2:
        return 0.0
    window = valid[:n_weeks + 1]
    newest, oldest = window[0], window[-1]
    if newest.review_count <= oldest.review_count:
        return 0.0
    elapsed = (newest.snapped_at - oldest.snapped_at).total_seconds() / 86400
    return (newest.review_count - oldest.review_count) / (elapsed / 7) if elapsed >= 1 else 0.0


def _build_velocity_projections(own_google_snaps: list, own_snap_deltas: dict, comp_rows: list) -> list:
    own_by_market = {s.market: s for s in own_google_snaps}
    dist_comps: dict = defaultdict(list)
    for r in comp_rows:
        dist_comps[r["district"]].append(r)

    projections = []
    for market, display, district in _OWN_MARKETS_META:
        snap = own_by_market.get(market)
        if not snap or snap.review_count is None:
            continue
        own_count = snap.review_count
        own_rate = own_snap_deltas.get(market, 0)

        rivals = dist_comps.get(district, [])
        if not rivals:
            continue
        top_rival = max(rivals, key=lambda r: r["google_count"] or 0)
        rival_count = top_rival["google_count"] or 0
        rival_rate = top_rival.get("weekly_rate") or 0

        gap = rival_count - own_count  # positive = rival ahead

        if gap > 0:
            rate_diff = own_rate - rival_rate  # positive = closing the gap
            if rate_diff > 0:
                weeks = gap / rate_diff
                if weeks < 52:
                    proj_text = f"Closing — ~{round(weeks)} wk to match"
                else:
                    proj_text = f"Closing slowly — ~{round(weeks / 52, 1)} yr"
                proj_type = "trailing_closing"
                proj_weeks = weeks
            else:
                proj_type = "trailing_widening"
                proj_weeks = None
                if rate_diff < 0:
                    proj_text = f"Gap widening — rival +{abs(round(rate_diff, 1))}/wk faster"
                else:
                    proj_text = "Static — matching rival's pace"
        elif gap < 0:
            rate_diff = rival_rate - own_rate  # positive = rival closing
            if rate_diff > 0:
                weeks = abs(gap) / rate_diff
                proj_type = "leading_threatened"
                if weeks < 52:
                    proj_text = f"At risk — rival catches up ~{round(weeks)} wk"
                else:
                    proj_text = f"Leading safely — ~{round(weeks / 52, 1)} yr buffer"
                proj_weeks = weeks
            else:
                proj_type = "leading_safe"
                proj_text = "Leading — rival not gaining ground"
                proj_weeks = None
        else:
            proj_type = "tied"
            proj_text = "Tied with top rival"
            proj_weeks = None

        pct = min(100, round(own_count / rival_count * 100)) if rival_count > 0 else 100
        rival_short = top_rival["name"] if len(top_rival["name"]) <= 28 else top_rival["name"][:27] + "…"

        projections.append({
            "market":      market,
            "display":     display,
            "district":    district,
            "own_count":   own_count,
            "own_rate":    own_rate,
            "rival_name":  rival_short,
            "rival_count": rival_count,
            "rival_rate":  rival_rate,
            "proj_type":   proj_type,
            "proj_text":   proj_text,
            "proj_weeks":  proj_weeks,
            "pct":         pct,
        })

    return projections


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
