from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.discovery import DiscoveryCache

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _yoy_pct(current: int, prior: int):
    if prior == 0:
        return None
    return round((current - prior) / prior * 100, 1)


@router.get("/consult-data", response_class=HTMLResponse)
def consult_data_page(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    damon_row = db.query(DiscoveryCache).filter(DiscoveryCache.key == "consultation_monthly_damon").first()
    anne_row  = db.query(DiscoveryCache).filter(DiscoveryCache.key == "consultation_monthly_anne").first()

    damon_data = damon_row.value if damon_row else {}
    anne_data  = anne_row.value  if anne_row  else {}

    damon_months = {(m["year"], m["month"]): m["count"] for m in damon_data.get("months", [])}
    anne_months  = {(m["year"], m["month"]): m["count"] for m in anne_data.get("months",  [])}

    all_years = sorted(set(y for (y, _) in list(damon_months) + list(anne_months)))

    # Annual totals
    annual_rows = []
    for year in all_years:
        d = sum(damon_months.get((year, m), 0) for m in range(1, 13))
        a = sum(anne_months.get((year, m), 0)  for m in range(1, 13))
        annual_rows.append({"year": year, "damon": d, "anne": a, "combined": d + a})

    # Monthly detail per year
    monthly_by_year = {}
    for year in all_years:
        rows = []
        for m in range(1, 13):
            d = damon_months.get((year, m), 0)
            a = anne_months.get((year, m), 0)
            if d > 0 or a > 0:
                rows.append({
                    "month_num": m,
                    "month": MONTH_NAMES[m - 1],
                    "damon": d,
                    "anne": a,
                    "combined": d + a,
                })
        monthly_by_year[year] = rows

    damon_alltime = sum(damon_months.values())

    # Last month with 2026 data (determines YTD period and trend anchor)
    m2026 = [m for (y, m) in list(damon_months.keys()) + list(anne_months.keys()) if y == 2026]
    last_2026_month = max(m2026) if m2026 else 5
    ytd_period = f"Jan–{MONTH_NAMES[last_2026_month - 1]}"

    # YTD totals (only through last available month, so comparisons are apples-to-apples)
    damon_2026_ytd = sum(damon_months.get((2026, m), 0) for m in range(1, last_2026_month + 1))
    anne_2026_ytd  = sum(anne_months.get((2026, m), 0)  for m in range(1, last_2026_month + 1))
    damon_2025_ytd = sum(damon_months.get((2025, m), 0) for m in range(1, last_2026_month + 1))
    # Anne started Oct 2025 — no Jan-May 2025 to compare against

    combined_2026_ytd = damon_2026_ytd + anne_2026_ytd
    combined_2025_ytd = damon_2025_ytd  # Anne had 0 for Jan-May 2025

    damon_2025 = sum(damon_months.get((2025, m), 0) for m in range(1, 13))
    anne_2025  = sum(anne_months.get((2025, m), 0)  for m in range(1, 13))

    damon_yoy_pct    = _yoy_pct(damon_2026_ytd, damon_2025_ytd)
    combined_yoy_pct = _yoy_pct(combined_2026_ytd, combined_2025_ytd)

    # 24-month trend (ending at last_2026_month)
    ref_total = 2026 * 12 + (last_2026_month - 1)
    trend_data = []
    for offset in range(23, -1, -1):
        t = ref_total - offset
        y, rem = divmod(t, 12)
        m = rem + 1
        d = damon_months.get((y, m), 0)
        a = anne_months.get((y, m), 0)
        trend_data.append({
            "label": f"{MONTH_NAMES[m - 1]} '{str(y)[-2:]}",
            "damon": d,
            "anne": a,
            "combined": d + a,
        })

    return templates.TemplateResponse("consult_data.html", {
        "request":            request,
        "user":               user,
        "active_page":        "consult-data",
        "annual_rows":        annual_rows,
        "monthly_by_year":    monthly_by_year,
        "all_years":          all_years,
        "damon_alltime":      damon_alltime,
        "damon_2026_ytd":     damon_2026_ytd,
        "anne_2026_ytd":      anne_2026_ytd,
        "combined_2026_ytd":  combined_2026_ytd,
        "damon_2025_ytd":     damon_2025_ytd,
        "combined_2025_ytd":  combined_2025_ytd,
        "damon_yoy_pct":      damon_yoy_pct,
        "combined_yoy_pct":   combined_yoy_pct,
        "ytd_period":         ytd_period,
        "damon_2025":         damon_2025,
        "anne_2025":          anne_2025,
        "trend_data":         trend_data,
        "notes":              damon_data.get("notes", []) + anne_data.get("notes", []),
        "updated_at":         damon_data.get("updated_at", ""),
    })
