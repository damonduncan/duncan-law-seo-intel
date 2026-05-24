from collections import defaultdict

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
    review_pair = (
        db.query(ReviewSnapshot)
        .filter(ReviewSnapshot.competitor_id == comp_id, ReviewSnapshot.source == "google")
        .order_by(ReviewSnapshot.snapped_at.desc())
        .limit(2)
        .all()
    )
    review_current = review_pair[0] if review_pair else None
    review_delta = None
    if (len(review_pair) >= 2
            and review_pair[0].review_count is not None
            and review_pair[1].review_count is not None):
        review_delta = review_pair[0].review_count - review_pair[1].review_count

    # ── Recent alerts ────────────────────────────────────────────────────────
    recent_alerts = (
        db.query(Alert)
        .filter(Alert.competitor_id == comp_id)
        .order_by(Alert.triggered_at.desc())
        .limit(10)
        .all()
    )

    return templates.TemplateResponse("competitor_detail.html", {
        "request":       request,
        "user":          user,
        "active_page":   "competitors",
        "comp":          comp,
        "ranking_as_of": ranking_as_of,
        "pack_presence": pack_presence,
        "pacer_data":    pacer_data,
        "review_current": review_current,
        "review_delta":  review_delta,
        "recent_alerts": recent_alerts,
    })
