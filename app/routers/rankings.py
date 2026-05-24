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


@router.get("/rankings", response_class=HTMLResponse)
def rankings(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    own_firm_id = own_firm.id if own_firm else None

    # Last 30 days of own-firm rankings (own-firm markets only)
    since = date.today() - timedelta(days=30)
    own_rankings = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm_id,
            LocalPackRanking.is_own_firm == True,
            cast(LocalPackRanking.scraped_at, Date) >= since,
        )
        .order_by(LocalPackRanking.scraped_at.desc())
        .all()
    ) if own_firm_id else []

    # Most recent snapshot per keyword for own firm
    latest_by_keyword = {}
    for r in own_rankings:
        key = (r.keyword, r.city)
        if key not in latest_by_keyword:
            latest_by_keyword[key] = r

    # Current 3-pack — own-firm markets only (for main table)
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
    # Convert nested defaultdicts to plain dicts for Jinja2
    ednc_by_market = {
        market: dict(kws)
        for market, kws in sorted(ednc_by_market.items())
    }

    # Build chart data: own firm position over time per market
    market_trend = defaultdict(lambda: defaultdict(list))
    for r in own_rankings:
        if r.rank_position:
            day_str = r.scraped_at.strftime("%Y-%m-%d")
            market_trend[r.market][day_str].append(r.rank_position)

    chart_data = {}
    for market, days in market_trend.items():
        sorted_days = sorted(days.items())
        chart_data[market] = {
            "labels": [d for d, _ in sorted_days],
            "data": [round(sum(v) / len(v), 1) for _, v in sorted_days],
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
        "current_pack": current_pack,
        "chart_data": chart_data,
        "own_firm": own_firm,
        "ednc_by_market": ednc_by_market,
        "EDNC_DISPLAY": EDNC_DISPLAY,
    })
