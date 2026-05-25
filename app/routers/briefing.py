from datetime import date
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.routers.overview import (
    _build_scorecard,
    _build_action_items,
    _build_pacer_share,
    _compute_filing_counts,
    _build_activity_feed,
)
from app.models.competitor import Competitor
from app.models.alerts import Alert

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _check_access(request: Request, token: str) -> bool:
    """Return True if the request is authorized to view the briefing."""
    if settings.briefing_token and token == settings.briefing_token:
        return True
    return bool(request.session.get("user"))


@router.get("/briefing", response_class=HTMLResponse)
def briefing(
    request: Request,
    token: str = Query(""),
    db: Session = Depends(get_db),
):
    if not _check_access(request, token):
        return RedirectResponse(url="/login", status_code=303)

    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    competitors = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).all()

    scorecard      = _build_scorecard(db, own_firm)
    action_items   = _build_action_items(db, own_firm, scorecard, competitors)
    fc_counts, fc_dist_latest = _compute_filing_counts(db)
    pacer_share    = _build_pacer_share(own_firm, fc_counts, fc_dist_latest)
    activity_feed  = _build_activity_feed(db, own_firm)

    unacked_count  = db.query(Alert).filter(Alert.acknowledged_at == None).count()
    total_kw       = sum(m["total"]   for m in scorecard)
    in_pack_kw     = sum(m["in_pack"] for m in scorecard)

    share_link = None
    if settings.briefing_token:
        base = settings.app_base_url.rstrip("/")
        share_link = f"{base}/briefing?token={settings.briefing_token}"

    return templates.TemplateResponse("briefing.html", {
        "request":       request,
        "own_firm":      own_firm,
        "scorecard":     scorecard,
        "action_items":  action_items[:5],
        "pacer_share":   pacer_share,
        "activity_feed": activity_feed[:15],
        "unacked_count": unacked_count,
        "total_kw":      total_kw,
        "in_pack_kw":    in_pack_kw,
        "generated":     date.today(),
        "share_link":    share_link,
        "token":         token,
    })
