from collections import defaultdict
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor, CompetitorAttorney
from app.models.filings import FilingSnapshot

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/filings", response_class=HTMLResponse)
def filings(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    has_data = db.query(FilingSnapshot).first() is not None

    if not has_data:
        return templates.TemplateResponse("filings.html", {
            "request": request,
            "user": user,
            "active_page": "filings",
            "has_data": False,
        })

    # Two most recent period_start values so we can show current + prior month
    periods = (
        db.query(FilingSnapshot.period_start)
        .distinct()
        .order_by(FilingSnapshot.period_start.desc())
        .limit(2)
        .all()
    )
    current_period = periods[0][0] if periods else None
    prior_period = periods[1][0] if len(periods) > 1 else None

    # Load all snapshots for both periods
    snapshots = (
        db.query(FilingSnapshot)
        .filter(FilingSnapshot.period_start.in_(
            [p[0] for p in periods]
        ))
        .all()
    )

    # attorney_id → CompetitorAttorney
    attorney_map = {
        a.id: a
        for a in db.query(CompetitorAttorney).all()
    }
    # competitor_id → Competitor
    comp_map = {
        c.id: c
        for c in db.query(Competitor).filter(Competitor.active == True).all()
    }

    # Build: (competitor_id, attorney_id, district, chapter, period) → count
    counts: dict = {}
    for s in snapshots:
        counts[(s.competitor_id, s.attorney_id, s.district, s.chapter, s.period_start)] = s.case_count

    # Build display rows grouped by district
    # Each row: firm, attorney, ch7_current, ch13_current, ch7_prior, ch13_prior, total_current
    def make_rows(district: str) -> list:
        keys = {
            (cid, aid)
            for (cid, aid, dist, ch, per) in counts
            if dist == district and per == current_period
        }
        rows = []
        for cid, aid in sorted(keys, key=lambda k: comp_map.get(k[0], Competitor()).name or ""):
            comp = comp_map.get(cid)
            atty = attorney_map.get(aid)
            if not comp or not atty:
                continue

            def get(ch, period):
                return counts.get((cid, aid, district, ch, period), 0)

            ch7_cur  = get(7, current_period)
            ch13_cur = get(13, current_period)
            ch7_pri  = get(7, prior_period) if prior_period else None
            ch13_pri = get(13, prior_period) if prior_period else None
            total_cur = ch7_cur + ch13_cur
            total_pri = (ch7_pri + ch13_pri) if prior_period else None

            pct_change = None
            if total_pri is not None and total_pri > 0:
                pct_change = round((total_cur - total_pri) / total_pri * 100)

            rows.append({
                "firm": comp.name,
                "attorney": atty.attorney_name,
                "ch7": ch7_cur,
                "ch13": ch13_cur,
                "total": total_cur,
                "ch7_prior": ch7_pri,
                "ch13_prior": ch13_pri,
                "total_prior": total_pri,
                "pct_change": pct_change,
                "is_own_firm": comp.is_own_firm,
            })

        rows.sort(key=lambda r: r["total"], reverse=True)
        return rows

    mdnc_rows = make_rows("MDNC")
    wdnc_rows = make_rows("WDNC")

    return templates.TemplateResponse("filings.html", {
        "request": request,
        "user": user,
        "active_page": "filings",
        "has_data": True,
        "current_period": current_period,
        "prior_period": prior_period,
        "mdnc_rows": mdnc_rows,
        "wdnc_rows": wdnc_rows,
    })
