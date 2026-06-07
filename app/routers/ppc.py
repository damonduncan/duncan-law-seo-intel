from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import cast, Date

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.discovery import DiscoveryCache
from app.models.rankings import LocalPackRanking
from app.models.competitor import Competitor
from app.models.base import new_uuid

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()

MARKET_DISPLAY = {
    "charlotte":    "Charlotte",
    "greensboro":   "Greensboro",
    "winston_salem": "Winston-Salem",
    "salisbury":    "Salisbury",
}
MARKETS = list(MARKET_DISPLAY.keys())
MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

MARKET_DISTRICT = {
    "charlotte":    "WDNC",
    "greensboro":   "MDNC",
    "winston_salem": "MDNC",
    "salisbury":    "MDNC",
}


def _month_label(year: int, month: int, fmt: str = "short") -> str:
    """Return 'Jan 2024' (short) or 'Jan '24' (chart) label."""
    abbr = MONTH_NAMES[month][:3]
    if fmt == "chart":
        return f"{abbr} '{str(year)[2:]}"
    return f"{abbr} {year}"


def _sort_key(m: dict) -> tuple:
    return (m["year"], m["month"])


@router.get("/ppc", response_class=HTMLResponse)
def ppc(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    # ── Load raw cache ────────────────────────────────────────────────────────
    cache_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "ppc_monthly_data"
    ).first()
    ppc_cache: list = (cache_row.value or []) if cache_row else []

    if not ppc_cache:
        return templates.TemplateResponse("ppc.html", {
            "request":       request,
            "user":          user,
            "active_page":   "ppc",
            "has_data":      False,
            "months":        [],
            "summary":       {},
            "chart_trend":   {},
            "chart_market_leads": {},
            "seasonal":      [],
            "market_summary": [],
            "recent_months": [],
            "organic_vs_ppc": [],
            "markets":       MARKETS,
            "market_display": MARKET_DISPLAY,
        })

    # ── Sort months oldest → newest, add label ────────────────────────────────
    months = sorted(ppc_cache, key=_sort_key)
    for m in months:
        m["label"] = _month_label(m["year"], m["month"])

    # ── Summary stats ─────────────────────────────────────────────────────────
    total_leads = sum(m.get("total", {}).get("leads", 0) or 0 for m in months)
    total_spend = sum(m.get("total", {}).get("spend", 0) or 0 for m in months)
    avg_cpl     = round(total_spend / total_leads, 2) if total_leads else None

    best_month_entry = max(
        months, key=lambda m: m.get("total", {}).get("leads", 0) or 0
    )
    best_month = {
        "label": best_month_entry["label"],
        "leads": best_month_entry.get("total", {}).get("leads", 0),
        "cpl":   best_month_entry.get("total", {}).get("cpl"),
    }

    latest_cpl = months[-1].get("total", {}).get("cpl") if months else None
    first_cpl  = months[0].get("total", {}).get("cpl")  if months else None
    if first_cpl and latest_cpl is not None and first_cpl != 0:
        cpl_improvement_pct = round((first_cpl - latest_cpl) / first_cpl * 100)
    else:
        cpl_improvement_pct = None

    trailing_12 = months[-12:]
    trailing_12_leads = sum(m.get("total", {}).get("leads", 0) or 0 for m in trailing_12)
    trailing_12_spend = sum(m.get("total", {}).get("spend", 0) or 0 for m in trailing_12)
    trailing_12_cpl   = (
        round(trailing_12_spend / trailing_12_leads, 2) if trailing_12_leads else None
    )

    summary = {
        "total_leads":         total_leads,
        "total_spend":         total_spend,
        "avg_cpl":             avg_cpl,
        "best_month":          best_month,
        "latest_cpl":          latest_cpl,
        "first_cpl":           first_cpl,
        "cpl_improvement_pct": cpl_improvement_pct,
        "trailing_12_leads":   trailing_12_leads,
        "trailing_12_spend":   trailing_12_spend,
        "trailing_12_cpl":     trailing_12_cpl,
    }

    # ── chart_trend: monthly leads + CPL + spend (all months) ────────────────
    chart_trend = {
        "labels": [_month_label(m["year"], m["month"], fmt="chart") for m in months],
        "leads":  [m.get("total", {}).get("leads") for m in months],
        "cpl":    [m.get("total", {}).get("cpl")   for m in months],
        "spend":  [m.get("total", {}).get("spend")  for m in months],
    }

    # ── chart_market_leads: stacked area, last 12 months ─────────────────────
    last_12 = months[-12:]
    chart_market_leads = {
        "labels": [_month_label(m["year"], m["month"], fmt="chart") for m in last_12],
        "series": [
            {
                "market":  mkt,
                "display": MARKET_DISPLAY[mkt],
                "data":    [m.get(mkt, {}).get("leads") for m in last_12],
            }
            for mkt in MARKETS
        ],
    }

    # ── seasonal: average leads + CPL by calendar month ──────────────────────
    seasonal_buckets: dict = defaultdict(lambda: {"leads": [], "cpl": []})
    for m in months:
        mo = m["month"]
        leads_val = m.get("total", {}).get("leads")
        cpl_val   = m.get("total", {}).get("cpl")
        if leads_val is not None:
            seasonal_buckets[mo]["leads"].append(leads_val)
        if cpl_val is not None:
            seasonal_buckets[mo]["cpl"].append(cpl_val)

    seasonal = []
    for mo in range(1, 13):
        if mo not in seasonal_buckets:
            continue
        leads_list = seasonal_buckets[mo]["leads"]
        cpl_list   = seasonal_buckets[mo]["cpl"]
        seasonal.append({
            "month_num":  mo,
            "month_name": MONTH_NAMES[mo],
            "avg_leads":  round(sum(leads_list) / len(leads_list), 1) if leads_list else None,
            "avg_cpl":    round(sum(cpl_list) / len(cpl_list), 2)    if cpl_list   else None,
        })

    # ── market_summary: aggregate per market across all months ───────────────
    market_summary = []
    for mkt in MARKETS:
        mkt_leads_list  = [m.get(mkt, {}).get("leads", 0) or 0 for m in months]
        mkt_spend_list  = [m.get(mkt, {}).get("spend", 0) or 0 for m in months]
        mkt_total_leads = sum(mkt_leads_list)
        mkt_total_spend = sum(mkt_spend_list)
        mkt_avg_cpl     = round(mkt_total_spend / mkt_total_leads, 2) if mkt_total_leads else None

        best_idx = max(range(len(mkt_leads_list)), key=lambda i: mkt_leads_list[i])
        market_summary.append({
            "market":           mkt,
            "display":          MARKET_DISPLAY[mkt],
            "total_leads":      mkt_total_leads,
            "total_spend":      mkt_total_spend,
            "avg_cpl":          mkt_avg_cpl,
            "best_month_leads": mkt_leads_list[best_idx] if months else 0,
            "best_month_label": months[best_idx]["label"] if months else None,
        })

    # ── recent_months: last 6 months with per-market breakdown ───────────────
    recent_months = months[-6:]

    # ── organic_vs_ppc: cross-analysis ───────────────────────────────────────
    since = date.today() - timedelta(days=7)
    latest_month = months[-1] if months else {}

    organic_vs_ppc = []
    for mkt in MARKETS:
        own_ranks = db.query(LocalPackRanking).filter(
            LocalPackRanking.is_own_firm == True,
            LocalPackRanking.in_pack == True,
            LocalPackRanking.market == mkt,
            cast(LocalPackRanking.scraped_at, Date) >= since,
        ).all()

        positions  = [r.rank_position for r in own_ranks if r.rank_position]
        avg_rank   = round(sum(positions) / len(positions), 1) if positions else None
        in_pack_pct = (
            round(len(positions) / max(len(own_ranks), 1) * 100) if own_ranks else None
        )

        if avg_rank is None:
            rank_label = "Not in pack"
        elif avg_rank <= 1.5:
            rank_label = "#1"
        elif avg_rank <= 2.5:
            rank_label = "#2"
        elif avg_rank <= 3.5:
            rank_label = "#3"
        else:
            rank_label = "Outside pack"

        mkt_data = latest_month.get(mkt, {}) if latest_month else {}
        organic_vs_ppc.append({
            "market":         mkt,
            "display":        MARKET_DISPLAY[mkt],
            "district":       MARKET_DISTRICT.get(mkt, "MDNC"),
            "avg_rank":       avg_rank,
            "in_pack_pct":    in_pack_pct,
            "monthly_spend":  mkt_data.get("spend"),
            "monthly_leads":  mkt_data.get("leads"),
            "cpl":            mkt_data.get("cpl"),
            "rank_label":     rank_label,
        })

    # ── Google Analytics 4 traffic data ──────────────────────────────────────
    ga_summary     = None
    ga_trend_chart = {"labels": [], "organic": [], "paid": [], "direct": []}
    try:
        from app.services.ga_service import get_ga_monthly_data
        ga_months = get_ga_monthly_data(db)
        if ga_months:
            latest_ga = ga_months[-1]
            prev_ga   = ga_months[-2] if len(ga_months) >= 2 else None
            total_s   = latest_ga.get("total_sessions", 0)
            total_o   = latest_ga.get("total_organic", 0)
            total_p   = latest_ga.get("total_paid", 0)
            total_d   = latest_ga.get("total_direct", 0)
            total_c   = latest_ga.get("total_conversions", 0)
            ga_summary = {
                "label":             _month_label(latest_ga["year"], latest_ga["month"]),
                "total_sessions":    total_s,
                "total_organic":     total_o,
                "total_paid":        total_p,
                "total_direct":      total_d,
                "total_conversions": total_c,
                "organic_pct":       round(total_o / total_s * 100) if total_s else 0,
                "sessions_mom":      (total_s - prev_ga.get("total_sessions", 0)) if prev_ga else None,
                "organic_mom":       (total_o - prev_ga.get("total_organic", 0)) if prev_ga else None,
            }
            ga_last_6 = ga_months[-6:]
            ga_trend_chart = {
                "labels":  [_month_label(m["year"], m["month"], fmt="chart") for m in ga_last_6],
                "organic": [m.get("total_organic", 0) for m in ga_last_6],
                "paid":    [m.get("total_paid", 0)    for m in ga_last_6],
                "direct":  [m.get("total_direct", 0)  for m in ga_last_6],
            }
            # Annotate each organic_vs_ppc row with GA market data
            for row in organic_vs_ppc:
                mkt_ga = latest_ga.get("markets", {}).get(row["market"], {})
                row["ga_organic_sessions"]    = mkt_ga.get("organic_sessions") or None
                row["ga_organic_conversions"] = mkt_ga.get("conversions") or None
        else:
            for row in organic_vs_ppc:
                row["ga_organic_sessions"]    = None
                row["ga_organic_conversions"] = None
    except Exception as _ga_err:
        import logging as _log
        _log.getLogger(__name__).warning(f"GA data unavailable: {_ga_err}")
        for row in organic_vs_ppc:
            row["ga_organic_sessions"]    = None
            row["ga_organic_conversions"] = None

    return templates.TemplateResponse("ppc.html", {
        "request":            request,
        "user":               user,
        "active_page":        "ppc",
        "has_data":           bool(ppc_cache),
        "months":             months,
        "summary":            summary,
        "chart_trend":        chart_trend,
        "chart_market_leads": chart_market_leads,
        "seasonal":           seasonal,
        "market_summary":     market_summary,
        "recent_months":      recent_months,
        "organic_vs_ppc":     organic_vs_ppc,
        "markets":            MARKETS,
        "market_display":     MARKET_DISPLAY,
        "ga_summary":         ga_summary,
        "ga_trend_chart":     ga_trend_chart,
    })


@router.post("/ppc/add-month")
def ppc_add_month(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
    year: int  = Form(...),
    month: int = Form(...),
    # Charlotte
    charlotte_impressions: float = Form(0),
    charlotte_clicks:      float = Form(0),
    charlotte_ctr:         float = Form(0),
    charlotte_spend:       float = Form(0),
    charlotte_leads:       float = Form(0),
    charlotte_cpl:         float = Form(0),
    # Greensboro
    greensboro_impressions: float = Form(0),
    greensboro_clicks:      float = Form(0),
    greensboro_ctr:         float = Form(0),
    greensboro_spend:       float = Form(0),
    greensboro_leads:       float = Form(0),
    greensboro_cpl:         float = Form(0),
    # Winston-Salem
    winston_salem_impressions: float = Form(0),
    winston_salem_clicks:      float = Form(0),
    winston_salem_ctr:         float = Form(0),
    winston_salem_spend:       float = Form(0),
    winston_salem_leads:       float = Form(0),
    winston_salem_cpl:         float = Form(0),
    # Salisbury
    salisbury_impressions: float = Form(0),
    salisbury_clicks:      float = Form(0),
    salisbury_ctr:         float = Form(0),
    salisbury_spend:       float = Form(0),
    salisbury_leads:       float = Form(0),
    salisbury_cpl:         float = Form(0),
):
    # Build per-market dicts from form fields
    market_fields = {
        "charlotte": {
            "impressions": charlotte_impressions,
            "clicks":      charlotte_clicks,
            "ctr":         charlotte_ctr,
            "spend":       charlotte_spend,
            "leads":       charlotte_leads,
            "cpl":         charlotte_cpl,
        },
        "greensboro": {
            "impressions": greensboro_impressions,
            "clicks":      greensboro_clicks,
            "ctr":         greensboro_ctr,
            "spend":       greensboro_spend,
            "leads":       greensboro_leads,
            "cpl":         greensboro_cpl,
        },
        "winston_salem": {
            "impressions": winston_salem_impressions,
            "clicks":      winston_salem_clicks,
            "ctr":         winston_salem_ctr,
            "spend":       winston_salem_spend,
            "leads":       winston_salem_leads,
            "cpl":         winston_salem_cpl,
        },
        "salisbury": {
            "impressions": salisbury_impressions,
            "clicks":      salisbury_clicks,
            "ctr":         salisbury_ctr,
            "spend":       salisbury_spend,
            "leads":       salisbury_leads,
            "cpl":         salisbury_cpl,
        },
    }

    # Compute totals across all markets
    total_impressions = sum(v["impressions"] for v in market_fields.values())
    total_clicks      = sum(v["clicks"]      for v in market_fields.values())
    total_spend       = sum(v["spend"]       for v in market_fields.values())
    total_leads       = sum(v["leads"]       for v in market_fields.values())
    total_cpl         = round(total_spend / total_leads, 2) if total_leads else 0
    total_ctr         = (
        round(total_clicks / total_impressions * 100, 2) if total_impressions else 0
    )

    new_entry = {
        "year":          year,
        "month":         month,
        **market_fields,
        "total": {
            "impressions": total_impressions,
            "clicks":      total_clicks,
            "ctr":         total_ctr,
            "spend":       total_spend,
            "leads":       total_leads,
            "cpl":         total_cpl,
        },
    }

    # Load existing cache
    cache_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "ppc_monthly_data"
    ).first()
    existing: list = (cache_row.value or []) if cache_row else []

    # Remove existing entry for same year/month if present (upsert behaviour)
    existing = [m for m in existing if not (m["year"] == year and m["month"] == month)]
    existing.append(new_entry)

    if cache_row:
        cache_row.value      = existing
        cache_row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(DiscoveryCache(
            id=new_uuid(),
            key="ppc_monthly_data",
            value=existing,
            updated_at=datetime.now(timezone.utc),
        ))

    db.commit()
    return RedirectResponse(url="/ppc?msg=added", status_code=303)


@router.get("/ppc/data.json")
def ppc_data_json(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    cache_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "ppc_monthly_data"
    ).first()
    data = (cache_row.value or []) if cache_row else []
    return JSONResponse(content={"months": data, "count": len(data)})
