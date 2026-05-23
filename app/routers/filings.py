from collections import defaultdict
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor, CompetitorAttorney
from app.models.filings import FilingSnapshot
from app.services.pacer_discovery import get_cached_results

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
    # Use MAX when duplicates exist (stale 0-rows from partial previous runs)
    counts: dict = {}
    for s in snapshots:
        key = (s.competitor_id, s.attorney_id, s.district, s.chapter, s.period_start)
        if key not in counts or s.case_count > counts[key]:
            counts[key] = s.case_count

    def make_firm_groups(district: str) -> list:
        """
        Build firm-level groups, each containing a firm rollup total and
        individual attorney rows sorted by cases filed. Own firm always first.
        """
        keys = {
            (cid, aid)
            for (cid, aid, dist, ch, per) in counts
            if dist == district and per == current_period
        }

        # Collect all attorney rows keyed by competitor_id
        firm_attorneys: dict = defaultdict(list)
        for cid, aid in keys:
            comp = comp_map.get(cid)
            atty = attorney_map.get(aid)
            if not comp or not atty:
                continue

            def get(c, aid=aid, cid=cid, period=None):
                return counts.get((cid, aid, district, c, period or current_period), 0)

            ch7_cur   = get(7)
            ch13_cur  = get(13)
            ch7_pri   = counts.get((cid, aid, district, 7,  prior_period), 0) if prior_period else None
            ch13_pri  = counts.get((cid, aid, district, 13, prior_period), 0) if prior_period else None
            total_cur = ch7_cur + ch13_cur
            total_pri = (ch7_pri + ch13_pri) if prior_period else None

            atty_pct = None
            if total_pri and total_pri > 0:
                atty_pct = round((total_cur - total_pri) / total_pri * 100)

            firm_attorneys[cid].append({
                "attorney":    atty.attorney_name,
                "ch7":         ch7_cur,
                "ch13":        ch13_cur,
                "total":       total_cur,
                "ch7_prior":   ch7_pri,
                "ch13_prior":  ch13_pri,
                "total_prior": total_pri,
                "pct_change":  atty_pct,
                "is_own_firm": comp.is_own_firm,
            })

        # Build firm groups
        groups = []
        for cid, atty_rows in firm_attorneys.items():
            comp = comp_map.get(cid)
            if not comp:
                continue

            atty_rows.sort(key=lambda r: r["total"], reverse=True)

            firm_ch7   = sum(r["ch7"]  for r in atty_rows)
            firm_ch13  = sum(r["ch13"] for r in atty_rows)
            firm_total = firm_ch7 + firm_ch13

            firm_pri = None
            if prior_period:
                firm_pri = sum(
                    (r["total_prior"] or 0) for r in atty_rows
                )
            pct_change = None
            if firm_pri:
                pct_change = round((firm_total - firm_pri) / firm_pri * 100)

            groups.append({
                "firm":        comp.name,
                "is_own_firm": comp.is_own_firm,
                "ch7":         firm_ch7,
                "ch13":        firm_ch13,
                "total":       firm_total,
                "total_prior": firm_pri,
                "pct_change":  pct_change,
                "attorneys":   atty_rows,
            })

        # Own firm first, then by total descending
        groups.sort(key=lambda g: (not g["is_own_firm"], -g["total"]))
        return groups

    mdnc_rows      = make_firm_groups("MDNC")
    wdnc_rows      = make_firm_groups("WDNC")
    ednc_rows      = make_firm_groups("EDNC")
    ednc_discovery = get_cached_results(db)

    # Add market-share % (share of tracked-firm cases in that district)
    for rows in (mdnc_rows, wdnc_rows, ednc_rows):
        district_total = sum(g["total"] for g in rows)
        for g in rows:
            g["market_share"] = round(g["total"] / district_total * 100) if district_total else 0

    return templates.TemplateResponse("filings.html", {
        "request": request,
        "user": user,
        "active_page": "filings",
        "has_data": True,
        "current_period": current_period,
        "prior_period": prior_period,
        "mdnc_rows":      mdnc_rows,
        "wdnc_rows":      wdnc_rows,
        "ednc_rows":      ednc_rows,
        "ednc_discovery": ednc_discovery,
    })
