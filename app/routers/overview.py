from datetime import date
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import cast, Date
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor
from app.models.alerts import Alert, JobRun
from app.models.rankings import LocalPackRanking
from app.models.reviews import ReviewSnapshot

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()

MARKET_ORDER = [
    "greensboro", "winston_salem", "high_point",
    "charlotte", "salisbury", "asheville",
]


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    competitors = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).all()
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    unacked_alerts = db.query(Alert).filter(Alert.acknowledged_at == None).count()
    last_job = db.query(JobRun).order_by(JobRun.started_at.desc()).first()

    scorecard = _build_scorecard(db, own_firm)

    return templates.TemplateResponse("overview.html", {
        "request": request,
        "user": user,
        "own_firm": own_firm,
        "competitors": competitors,
        "unacked_alerts": unacked_alerts,
        "last_job": last_job,
        "scorecard": scorecard,
        "active_page": "dashboard",
    })


def _build_scorecard(db: Session, own_firm) -> list:
    if not own_firm:
        return []

    # Today's own-firm rankings — count in_pack vs total per market
    today = date.today()
    rank_rows = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm.id,
            LocalPackRanking.is_own_firm == True,
            cast(LocalPackRanking.scraped_at, Date) == today,
        )
        .all()
    )
    pack_by_market: dict = {}
    for r in rank_rows:
        m = pack_by_market.setdefault(r.market, {"in_pack": 0, "total": 0})
        m["total"] += 1
        if r.in_pack:
            m["in_pack"] += 1

    # Latest Google review count per own-firm market
    review_snaps = (
        db.query(ReviewSnapshot)
        .filter(
            ReviewSnapshot.competitor_id == own_firm.id,
            ReviewSnapshot.source == "google",
            ReviewSnapshot.market != None,
        )
        .order_by(ReviewSnapshot.snapped_at.desc())
        .all()
    )
    reviews_by_market: dict = {}
    for s in review_snaps:
        if s.market not in reviews_by_market:
            reviews_by_market[s.market] = s.review_count or 0

    scorecard = []
    for market in MARKET_ORDER:
        kw = pack_by_market.get(market, {"in_pack": 0, "total": 0})
        reviews = reviews_by_market.get(market)
        in_pack = kw["in_pack"]
        total = kw["total"]

        if total == 0 and reviews is None:
            status = "no_data"
        elif in_pack == total and total > 0 and (reviews or 0) >= 30:
            status = "strong"
        elif in_pack >= (total // 2 if total else 1) and (reviews or 0) >= 10:
            status = "ok"
        else:
            status = "needs_attention"

        scorecard.append({
            "market": market,
            "display": market.replace("_", " ").title(),
            "in_pack": in_pack,
            "total": total,
            "reviews": reviews,
            "status": status,
        })

    return scorecard
