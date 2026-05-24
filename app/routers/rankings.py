from datetime import date, timedelta
from collections import defaultdict
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import cast, Date
from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.rankings import LocalPackRanking
from app.models.competitor import Competitor

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()

OWN_FIRM_MARKETS = {"greensboro", "winston_salem", "high_point", "charlotte", "salisbury", "asheville"}
EDNC_MARKETS = {"raleigh", "fayetteville", "wilmington", "wilson"}
EDNC_DISPLAY = {
    "raleigh": "Raleigh",
    "fayetteville": "Fayetteville",
    "wilmington": "Wilmington",
    "wilson": "Wilson",
}
MARKET_ORDER = ["greensboro", "winston_salem", "high_point", "charlotte", "salisbury", "asheville"]

_CITY_SUFFIXES = {
    "greensboro": [" Greensboro"],
    "winston_salem": [" Winston-Salem", " Winston Salem"],
    "high_point": [" High Point"],
    "charlotte": [" Charlotte"],
    "salisbury": [" Salisbury"],
    "asheville": [" Asheville"],
}


def _strip_city(kw: str, market: str) -> str:
    for suffix in _CITY_SUFFIXES.get(market, []):
        if kw.lower().endswith(suffix.lower()):
            return kw[:len(kw) - len(suffix)].strip()
    return kw


@router.get("/rankings", response_class=HTMLResponse)
def rankings(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    own_firm_id = own_firm.id if own_firm else None

    # 90 days of own-firm rankings (own-firm markets only), ascending for trend building
    since = date.today() - timedelta(days=90)
    own_rankings = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm_id,
            LocalPackRanking.is_own_firm == True,
            LocalPackRanking.market.in_(OWN_FIRM_MARKETS),
            cast(LocalPackRanking.scraped_at, Date) >= since,
        )
        .order_by(LocalPackRanking.scraped_at.asc())
        .all()
    ) if own_firm_id else []

    # Latest snapshot per keyword for position matrix (last write wins since asc order)
    latest_by_keyword: dict = {}
    for r in own_rankings:
        latest_by_keyword[(r.keyword, r.city)] = r

    # Build trend: market → kw_short → {date_str: rank | None}
    trend_raw: dict = defaultdict(lambda: defaultdict(dict))
    all_dates: set = set()
    for r in own_rankings:
        day_str = r.scraped_at.strftime("%Y-%m-%d")
        all_dates.add(day_str)
        kw = _strip_city(r.keyword or "", r.market)
        trend_raw[r.market][kw][day_str] = r.rank_position if r.in_pack else None

    sorted_dates = sorted(all_dates)
    date_labels = [d[5:].replace("-", "/") for d in sorted_dates]  # "MM/DD"
    kw_colors = ["#2563EB", "#10B981", "#F97316", "#8B5CF6"]

    chart_data: dict = {}
    for market in MARKET_ORDER:
        if market not in trend_raw:
            continue
        series = []
        for i, (kw, day_ranks) in enumerate(sorted(trend_raw[market].items())):
            series.append({
                "label": kw,
                "color": kw_colors[i % len(kw_colors)],
                "data": [day_ranks.get(d) for d in sorted_dates],
            })
        if series:
            chart_data[market] = {"labels": date_labels, "series": series}

    # Week-over-week delta: market → kw_short → {current, prior, delta}
    week_ago_str = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_delta: dict = {}
    for market, kw_dict in trend_raw.items():
        week_delta[market] = {}
        for kw, day_ranks in kw_dict.items():
            date_keys = sorted(day_ranks.keys())
            current_rank = day_ranks[date_keys[-1]] if date_keys else None
            prior_days = [d for d in date_keys if d <= week_ago_str]
            prior_rank = day_ranks[prior_days[-1]] if prior_days else None
            delta = (current_rank - prior_rank
                     if current_rank is not None and prior_rank is not None
                     else None)
            week_delta[market][kw] = {"current": current_rank, "prior": prior_rank, "delta": delta}

    # positions list — drives Current Positions table with pre-computed delta
    seen_pos: set = set()
    positions = []
    for r in reversed(own_rankings):  # reversed → latest first
        key = (r.keyword, r.city)
        if key in seen_pos:
            continue
        seen_pos.add(key)
        kw_short = _strip_city(r.keyword or "", r.market)
        delta_info = week_delta.get(r.market, {}).get(kw_short)
        positions.append({
            "keyword": r.keyword,
            "keyword_short": kw_short,
            "city": r.city,
            "market": r.market,
            "in_pack": r.in_pack,
            "rank_position": r.rank_position,
            "scraped_at": r.scraped_at,
            "delta": delta_info,
        })
    positions.sort(key=lambda x: (x["keyword"] or "", x["city"] or ""))

    # Current 3-pack — own-firm markets only (for full pack table)
    today = date.today()
    current_pack = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.in_pack == True,
            LocalPackRanking.market.in_(OWN_FIRM_MARKETS),
            cast(LocalPackRanking.scraped_at, Date) == today,
        )
        .order_by(LocalPackRanking.market, LocalPackRanking.keyword, LocalPackRanking.rank_position)
        .all()
    )

    # EDNC competitor pack — grouped by market → keyword → ranked firms
    ednc_rows = (
        db.query(LocalPackRanking, Competitor)
        .join(Competitor, LocalPackRanking.competitor_id == Competitor.id)
        .filter(
            LocalPackRanking.in_pack == True,
            LocalPackRanking.market.in_(EDNC_MARKETS),
            cast(LocalPackRanking.scraped_at, Date) == today,
        )
        .order_by(LocalPackRanking.market, LocalPackRanking.keyword, LocalPackRanking.rank_position)
        .all()
    )

    ednc_by_market = defaultdict(lambda: defaultdict(list))
    for ranking, comp in ednc_rows:
        firm_name = (
            ranking.result_data.get("title") if ranking.result_data else None
        ) or comp.name
        ednc_by_market[ranking.market][ranking.keyword].append({
            "rank": ranking.rank_position,
            "name": firm_name,
        })
    ednc_by_market = {
        market: dict(kws)
        for market, kws in sorted(ednc_by_market.items())
    }

    in_pack_count = sum(1 for r in latest_by_keyword.values() if r.in_pack)
    total_keywords = len(latest_by_keyword)
    has_data = total_keywords > 0

    return templates.TemplateResponse("rankings.html", {
        "request": request,
        "user": user,
        "active_page": "rankings",
        "has_data": has_data,
        "in_pack_count": in_pack_count,
        "total_keywords": total_keywords,
        "latest_by_keyword": dict(latest_by_keyword),
        "positions": positions,
        "current_pack": current_pack,
        "chart_data": chart_data,
        "own_firm": own_firm,
        "ednc_by_market": ednc_by_market,
        "EDNC_DISPLAY": EDNC_DISPLAY,
        "MARKET_ORDER": MARKET_ORDER,
    })
