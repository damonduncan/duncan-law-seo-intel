from collections import defaultdict
from fastapi import APIRouter, Request, Depends, Query
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
    win: str = Query("1m"),
):
    has_data = db.query(FilingSnapshot).first() is not None

    if not has_data:
        return templates.TemplateResponse("filings.html", {
            "request": request,
            "user": user,
            "active_page": "filings",
            "has_data": False,
        })

    all_snapshots = db.query(FilingSnapshot).all()

    attorney_map = {a.id: a for a in db.query(CompetitorAttorney).all()}
    comp_map = {
        c.id: c
        for c in db.query(Competitor).filter(Competitor.active == True).all()
    }

    # De-dupe: max case_count per (competitor_id, attorney_id, district, chapter, period_start)
    counts: dict = {}
    for s in all_snapshots:
        key = (s.competitor_id, s.attorney_id, s.district, s.chapter, s.period_start)
        if key not in counts or s.case_count > counts[key]:
            counts[key] = s.case_count

    # All distinct periods, newest first
    distinct_periods = sorted(set(per for (_, _, _, _, per) in counts), reverse=True)

    # Validate + resolve window slice
    if win not in {"1m", "3m", "6m", "12m", "all"}:
        win = "1m"
    win_slices = {
        "1m":  (distinct_periods[:1],  distinct_periods[1:2]),
        "3m":  (distinct_periods[:3],  distinct_periods[3:6]),
        "6m":  (distinct_periods[:6],  distinct_periods[6:12]),
        "12m": (distinct_periods[:12], distinct_periods[12:24]),
        "all": (distinct_periods[:],   []),
    }
    incl_periods, cmp_periods = win_slices[win]

    def _period_label(periods):
        if not periods:
            return None
        if len(periods) == 1:
            return periods[0].strftime("%b %Y")
        oldest, newest = periods[-1], periods[0]
        if oldest.year == newest.year:
            return f"{oldest.strftime('%b')} – {newest.strftime('%b %Y')}"
        return f"{oldest.strftime('%b %Y')} – {newest.strftime('%b %Y')}"

    cur_label   = "All time" if win == "all" else _period_label(incl_periods)
    prior_label = _period_label(cmp_periods) if cmp_periods else None

    def compute_window(district: str, incl: list, cmp: list) -> list:
        incl_set = set(incl)
        cmp_set  = set(cmp)
        if not incl_set:
            return []

        keys = {
            (cid, aid)
            for (cid, aid, dist, ch, per) in counts
            if dist == district and per in incl_set
        }

        firm_attorneys: dict = defaultdict(list)
        for cid, aid in keys:
            comp = comp_map.get(cid)
            atty = attorney_map.get(aid)
            if not comp or not atty:
                continue

            ch7_cur  = sum(counts.get((cid, aid, district, 7,  p), 0) for p in incl)
            ch13_cur = sum(counts.get((cid, aid, district, 13, p), 0) for p in incl)
            total_cur = ch7_cur + ch13_cur

            if cmp_set:
                ch7_pri  = sum(counts.get((cid, aid, district, 7,  p), 0) for p in cmp)
                ch13_pri = sum(counts.get((cid, aid, district, 13, p), 0) for p in cmp)
                total_pri = ch7_pri + ch13_pri
            else:
                ch7_pri = ch13_pri = total_pri = None

            atty_pct = (round((total_cur - total_pri) / total_pri * 100)
                        if (total_pri and total_pri > 0) else None)

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

        groups = []
        for cid, atty_rows in firm_attorneys.items():
            comp = comp_map.get(cid)
            if not comp:
                continue
            atty_rows.sort(key=lambda r: r["total"], reverse=True)

            firm_ch7   = sum(r["ch7"]  for r in atty_rows)
            firm_ch13  = sum(r["ch13"] for r in atty_rows)
            firm_total = firm_ch7 + firm_ch13
            firm_pri   = sum((r["total_prior"] or 0) for r in atty_rows) if cmp_set else None
            pct_change = (round((firm_total - firm_pri) / firm_pri * 100)
                          if (firm_pri and firm_pri > 0) else None)

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

        groups.sort(key=lambda g: (not g["is_own_firm"], -g["total"]))
        district_total = sum(g["total"] for g in groups)
        for g in groups:
            g["market_share"] = round(g["total"] / district_total * 100) if district_total else 0
        return groups

    mdnc_rows = compute_window("MDNC", incl_periods, cmp_periods)
    wdnc_rows = compute_window("WDNC", incl_periods, cmp_periods)
    ednc_rows = compute_window("EDNC", incl_periods, cmp_periods)

    # Aggregate totals per (competitor, district, period) for trend charts — uses all periods
    firm_period_totals: dict = defaultdict(int)
    for (cid, _aid, dist, _ch, per), count in counts.items():
        firm_period_totals[(cid, dist, per)] += count

    def build_trend(district: str) -> dict:
        pairs = [(cid, per) for (cid, dist, per) in firm_period_totals if dist == district]
        if not pairs:
            return {"labels": [], "series": []}
        district_periods = sorted(set(per for (_, per) in pairs))
        if len(district_periods) < 2:
            return {"labels": [], "series": []}
        labels = [p.strftime("%b '%y") for p in district_periods]
        series = []
        for cid in set(cid for (cid, _) in pairs):
            comp = comp_map.get(cid)
            if not comp:
                continue
            data = [firm_period_totals.get((cid, district, per), 0) for per in district_periods]
            if any(d > 0 for d in data):
                name = comp.name if len(comp.name) <= 24 else comp.name[:23] + "…"
                series.append({"firm": name, "is_own": comp.is_own_firm, "data": data})
        series.sort(key=lambda s: (not s["is_own"], -(s["data"][-1] if s["data"] else 0)))
        return {"labels": labels, "series": series}

    mdnc_trend = build_trend("MDNC")
    wdnc_trend = build_trend("WDNC")
    ednc_trend = build_trend("EDNC")

    mdnc_discovery = get_cached_results(db, "MDNC")
    wdnc_discovery = get_cached_results(db, "WDNC")
    ednc_discovery = get_cached_results(db, "EDNC")

    from app.services.pacer import MARKET_TO_DISTRICT
    tracked_last_names: dict = {"MDNC": set(), "WDNC": set(), "EDNC": set()}
    for comp in comp_map.values():
        for loc in comp.locations:
            d = MARKET_TO_DISTRICT.get(loc.market)
            if d in tracked_last_names:
                for atty in comp.attorneys:
                    parts = atty.attorney_name.strip().split()
                    if parts:
                        tracked_last_names[d].add(parts[-1].lower())

    return templates.TemplateResponse("filings.html", {
        "request":        request,
        "user":           user,
        "active_page":    "filings",
        "has_data":       True,
        "win":            win,
        "cur_label":      cur_label,
        "prior_label":    prior_label,
        "mdnc_rows":      mdnc_rows,
        "wdnc_rows":      wdnc_rows,
        "ednc_rows":      ednc_rows,
        "mdnc_discovery": mdnc_discovery,
        "wdnc_discovery": wdnc_discovery,
        "ednc_discovery": ednc_discovery,
        "mdnc_tracked":   tracked_last_names["MDNC"],
        "wdnc_tracked":   tracked_last_names["WDNC"],
        "ednc_tracked":   tracked_last_names["EDNC"],
        "mdnc_trend":     mdnc_trend,
        "wdnc_trend":     wdnc_trend,
        "ednc_trend":     ednc_trend,
    })
