import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.discovery import DiscoveryCache
from app.models.base import new_uuid

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()
logger = logging.getLogger(__name__)

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _yoy_pct(current: int, prior: int):
    if prior == 0:
        return None
    return round((current - prior) / prior * 100, 1)


def _generate_insights(damon_months: dict, anne_months: dict) -> list:
    """Call Claude to produce 4–5 analytical insights from the consultation data."""
    from app.config import settings
    import anthropic

    if not settings.anthropic_api_key:
        return []

    all_years = sorted(set(y for (y, _) in list(damon_months.keys()) + list(anne_months.keys())))

    # Annual totals
    annual_lines = []
    for year in all_years:
        d = sum(damon_months.get((year, m), 0) for m in range(1, 13))
        a = sum(anne_months.get((year, m), 0)  for m in range(1, 13))
        annual_lines.append(f"  {year}: Damon={d}, Anne={a}, Total={d + a}")

    # Seasonality — Damon's average by calendar month across full years (2010–2025)
    full_years = [y for y in range(2010, 2026) if sum(damon_months.get((y, m), 0) for m in range(1, 13)) > 0]
    season_lines = []
    for m in range(1, 13):
        vals = [damon_months.get((y, m), 0) for y in full_years if damon_months.get((y, m)) is not None]
        if vals:
            avg = sum(vals) / len(vals)
            season_lines.append(f"  {MONTH_NAMES[m - 1]}: avg {avg:.1f} ({len(vals)} yrs of data)")

    # YTD comparison (Jan–May)
    d_2026 = sum(damon_months.get((2026, m), 0) for m in range(1, 6))
    d_2025 = sum(damon_months.get((2025, m), 0) for m in range(1, 6))
    a_2026 = sum(anne_months.get((2026, m), 0)  for m in range(1, 6))

    # Anne's ramp — last 8 months
    anne_recent = sorted([(y, m, anne_months[(y, m)]) for (y, m) in anne_months], key=lambda x: (x[0], x[1]))
    anne_lines  = [f"  {MONTH_NAMES[m - 1]} {y}: {c}" for y, m, c in anne_recent]

    prompt = f"""You are a practice management analyst for Duncan Law LLP, a consumer bankruptcy law firm in
Charlotte, NC. Damon Duncan is the founding attorney; Anne Salter joined in October 2025.

CRITICAL CONTEXT — read before analyzing:
- Damon deliberately shifted from doing consultations to doing signing appointments as Anne ramped up.
  His declining individual consult count is NOT a negative trend — it reflects successful delegation.
- Signing appointments (Damon's new focus) are the step where a prospect becomes a paying client and
  a case gets filed. More signing appointments = more cases filed = more revenue.
- The right measure of intake pipeline health is COMBINED consultation volume (Damon + Anne), not
  Damon's count alone. Never flag Damon's declining share as a problem.
- Damon reserves Tuesdays as administrative/business-development days with no client appointments.
  This intentionally reduces his available consult slots by roughly one day per week.
- A pattern where Anne's consults rise while Damon's fall is the intended, healthy outcome.

Analyze the data below and return exactly 5 concise, specific, actionable insights useful to the
firm's principals — covering combined intake trends, seasonality, Anne's ramp trajectory, capacity
for additional volume, and forecasts. Each insight must reference specific numbers.
Write for attorneys, not data scientists. Do not flag Damon's individual decline as a concern.

ANNUAL CONSULTATION TOTALS:
{chr(10).join(annual_lines)}

DAMON'S HISTORICAL AVERAGE BY CALENDAR MONTH (2010–2025 full years only):
{chr(10).join(season_lines)}

2026 YTD JAN–MAY:  Damon={d_2026}, Anne={a_2026}, Combined={d_2026 + a_2026}
2025 YTD JAN–MAY:  Damon={d_2025} (Anne not yet hired)

ANNE'S MONTHLY COUNTS SINCE JOINING:
{chr(10).join(anne_lines)}

Return ONLY a valid JSON array (no markdown, no explanation) with 5 objects, each having:
  "category": one of trend | seasonality | capacity | forecast | comparison
  "text": 1–2 sentences, specific numbers, actionable tone
  "sentiment": positive | negative | neutral"""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rstrip("`").strip()

    return json.loads(raw)


def _get_or_refresh_insights(db: Session, damon_months: dict, anne_months: dict,
                              force: bool = False) -> tuple[list, str]:
    """Return (insights_list, updated_label). Regenerates if stale (>30 days) or forced."""
    row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "consultation_insights"
    ).first()

    if not force and row and row.updated_at:
        age = datetime.now(timezone.utc) - row.updated_at.replace(tzinfo=timezone.utc)
        if age < timedelta(days=30):
            label = row.updated_at.strftime("%b %d, %Y")
            return row.value or [], label

    try:
        insights = _generate_insights(damon_months, anne_months)
        now = datetime.now(timezone.utc)
        if row:
            row.value      = insights
            row.updated_at = now
        else:
            db.add(DiscoveryCache(id=new_uuid(), key="consultation_insights",
                                  value=insights, updated_at=now))
        db.commit()
        return insights, now.strftime("%b %d, %Y")
    except Exception as e:
        logger.error(f"Failed to generate consultation insights: {e}", exc_info=True)
        if row:
            return row.value or [], row.updated_at.strftime("%b %d, %Y")
        return [], ""


def _bg_refresh_insights(damon_months: dict, anne_months: dict) -> None:
    """Background task — opens its own DB session so the HTTP response is not blocked."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        _get_or_refresh_insights(db, damon_months, anne_months, force=True)
    except Exception as e:
        logger.error(f"Background insight refresh failed: {e}", exc_info=True)
    finally:
        db.close()


@router.post("/consult-data/refresh-insights")
def refresh_insights(
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    damon_row = db.query(DiscoveryCache).filter(DiscoveryCache.key == "consultation_monthly_damon").first()
    anne_row  = db.query(DiscoveryCache).filter(DiscoveryCache.key == "consultation_monthly_anne").first()
    damon_data = damon_row.value if damon_row else {}
    anne_data  = anne_row.value  if anne_row  else {}
    damon_months = {(m["year"], m["month"]): m["count"] for m in damon_data.get("months", [])}
    anne_months  = {(m["year"], m["month"]): m["count"] for m in anne_data.get("months",  [])}
    background_tasks.add_task(_bg_refresh_insights, damon_months, anne_months)
    return RedirectResponse(url="/consult-data?refreshing=1", status_code=303)


def _build_annual_rows(d_months, a_months, all_years):
    rows = []
    for year in all_years:
        d = sum(d_months.get((year, m), 0) for m in range(1, 13))
        a = sum(a_months.get((year, m), 0) for m in range(1, 13))
        rows.append({"year": year, "damon": d, "anne": a, "combined": d + a})
    return rows


def _build_monthly_by_year(d_months, a_months, all_years):
    by_year = {}
    for year in all_years:
        rows = []
        for m in range(1, 13):
            d = d_months.get((year, m), 0)
            a = a_months.get((year, m), 0)
            if d > 0 or a > 0:
                rows.append({"month_num": m, "month": MONTH_NAMES[m - 1],
                             "damon": d, "anne": a, "combined": d + a})
        by_year[year] = rows
    return by_year


def _build_trend(d_months, a_months, last_year, last_month, n=24):
    ref_total = last_year * 12 + (last_month - 1)
    data = []
    for offset in range(n - 1, -1, -1):
        t = ref_total - offset
        y, rem = divmod(t, 12)
        m = rem + 1
        d = d_months.get((y, m), 0)
        a = a_months.get((y, m), 0)
        data.append({"label": f"{MONTH_NAMES[m - 1]} '{str(y)[-2:]}",
                     "damon": d, "anne": a, "combined": d + a})
    return data


def _build_contract_trend(c_months, last_year, last_month, n=24):
    ref_total = last_year * 12 + (last_month - 1)
    data = []
    for offset in range(n - 1, -1, -1):
        t = ref_total - offset
        y, rem = divmod(t, 12)
        m = rem + 1
        data.append({"label": f"{MONTH_NAMES[m - 1]} '{str(y)[-2:]}",
                     "count": c_months.get((y, m), 0)})
    return data


def _build_annual_rows_single(c_months, all_years):
    return [{"year": y, "count": sum(c_months.get((y, m), 0) for m in range(1, 13))}
            for y in all_years]


def _build_monthly_by_year_single(c_months, all_years):
    by_year = {}
    for year in all_years:
        rows = []
        for m in range(1, 13):
            c = c_months.get((year, m), 0)
            if c > 0:
                rows.append({"month_num": m, "month": MONTH_NAMES[m - 1], "count": c})
        by_year[year] = rows
    return by_year


@router.get("/consult-data", response_class=HTMLResponse)
def consult_data_page(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    def _load(key):
        import json as _json
        row = db.query(DiscoveryCache).filter(DiscoveryCache.key == key).first()
        data = row.value if row else {}
        if isinstance(data, str):
            data = _json.loads(data)
        return {(m["year"], m["month"]): m["count"] for m in data.get("months", [])}, data

    damon_months, damon_data  = _load("consultation_monthly_damon")
    anne_months,  anne_data   = _load("consultation_monthly_anne")
    sign_d_months, _          = _load("signing_monthly_damon")
    sign_a_months, _          = _load("signing_monthly_anne")
    contract_months, _        = _load("docusign_monthly_contracts")

    all_consult_years = sorted(set(y for (y, _) in list(damon_months) + list(anne_months)))
    all_sign_years    = sorted(set(y for (y, _) in list(sign_d_months) + list(sign_a_months)))

    # YTD anchor — last month with 2026 data across all datasets
    m2026 = [m for (y, m) in list(damon_months) + list(anne_months)
                           + list(sign_d_months) + list(sign_a_months) if y == 2026]
    last_2026_month = max(m2026) if m2026 else 5
    ytd_period = f"Jan–{MONTH_NAMES[last_2026_month - 1]}"

    # ── Consultation stats ────────────────────────────────────────────────────
    annual_rows    = _build_annual_rows(damon_months, anne_months, all_consult_years)
    monthly_by_year = _build_monthly_by_year(damon_months, anne_months, all_consult_years)

    damon_alltime    = sum(damon_months.values())
    damon_2026_ytd   = sum(damon_months.get((2026, m), 0) for m in range(1, last_2026_month + 1))
    anne_2026_ytd    = sum(anne_months.get((2026, m), 0)  for m in range(1, last_2026_month + 1))
    damon_2025_ytd   = sum(damon_months.get((2025, m), 0) for m in range(1, last_2026_month + 1))
    combined_2026_ytd = damon_2026_ytd + anne_2026_ytd
    combined_2025_ytd = damon_2025_ytd
    damon_2025        = sum(damon_months.get((2025, m), 0) for m in range(1, 13))
    anne_2025         = sum(anne_months.get((2025, m), 0)  for m in range(1, 13))
    damon_yoy_pct     = _yoy_pct(damon_2026_ytd, damon_2025_ytd)
    combined_yoy_pct  = _yoy_pct(combined_2026_ytd, combined_2025_ytd)
    trend_data        = _build_trend(damon_months, anne_months, 2026, last_2026_month)

    # ── Signing stats ─────────────────────────────────────────────────────────
    sign_annual_rows     = _build_annual_rows(sign_d_months, sign_a_months, all_sign_years)
    sign_monthly_by_year = _build_monthly_by_year(sign_d_months, sign_a_months, all_sign_years)

    sign_damon_alltime   = sum(sign_d_months.values())
    sign_damon_2026_ytd  = sum(sign_d_months.get((2026, m), 0) for m in range(1, last_2026_month + 1))
    sign_anne_2026_ytd   = sum(sign_a_months.get((2026, m), 0) for m in range(1, last_2026_month + 1))
    sign_damon_2025_ytd  = sum(sign_d_months.get((2025, m), 0) for m in range(1, last_2026_month + 1))
    sign_combined_ytd    = sign_damon_2026_ytd + sign_anne_2026_ytd
    sign_damon_2025      = sum(sign_d_months.get((2025, m), 0) for m in range(1, 13))
    sign_damon_yoy_pct   = _yoy_pct(sign_damon_2026_ytd, sign_damon_2025_ytd)
    sign_trend_data      = _build_trend(sign_d_months, sign_a_months, 2026, last_2026_month)

    # Conversion rate: signings / consultations (combined, YTD)
    conv_rate_2026 = round(sign_combined_ytd / combined_2026_ytd * 100, 1) if combined_2026_ytd else None
    conv_rate_2025 = round(sign_damon_2025 / damon_2025 * 100, 1) if damon_2025 else None

    # ── DocuSign contracts ────────────────────────────────────────────────────
    contract_all_years        = sorted(set(y for (y, _) in contract_months))
    contracts_alltime         = sum(contract_months.values())
    contracts_2026_ytd        = sum(contract_months.get((2026, m), 0) for m in range(1, last_2026_month + 1))
    contracts_2025_ytd        = sum(contract_months.get((2025, m), 0) for m in range(1, last_2026_month + 1))
    contracts_2025            = sum(contract_months.get((2025, m), 0) for m in range(1, 13))
    contracts_yoy_pct         = _yoy_pct(contracts_2026_ytd, contracts_2025_ytd)
    contract_trend_data       = _build_contract_trend(contract_months, 2026, last_2026_month)
    contract_annual_rows      = _build_annual_rows_single(contract_months, contract_all_years)
    contract_monthly_by_year  = _build_monthly_by_year_single(contract_months, contract_all_years)
    # Consult → Contract conversion rate (contracts / combined consultations)
    combined_2025_total       = damon_2025 + anne_2025
    contract_conv_rate_2026   = round(contracts_2026_ytd / combined_2026_ytd * 100, 1) if combined_2026_ytd else None
    contract_conv_rate_2025   = round(contracts_2025 / combined_2025_total * 100, 1) if combined_2025_total else None
    # ── PACER filings for funnel ──────────────────────────────────────────────
    _filing_row = db.query(DiscoveryCache).filter(DiscoveryCache.key == "duncan_law_filing_history").first()
    _filing_hist = _filing_row.value if _filing_row else {}
    if isinstance(_filing_hist, str):
        import json as _j; _filing_hist = _j.loads(_filing_hist)
    _pacer_2026 = next((r for r in _filing_hist.get("annual", []) if r["year"] == 2026), None)
    pacer_filed_ytd = 0
    if _pacer_2026:
        _monthly = _pacer_2026.get("monthly") or []
        pacer_filed_ytd = sum(_monthly[m - 1] or 0 for m in range(1, last_2026_month + 1) if m - 1 < len(_monthly))

    # Contract → Filed rate (PACER filings / contracts signed)
    contract_to_filed_2026    = round(pacer_filed_ytd / contracts_2026_ytd * 100, 1) if contracts_2026_ytd else None
    # Overall consult → filed rate using PACER
    pacer_consult_rate_2026   = round(pacer_filed_ytd / combined_2026_ytd * 100, 1) if combined_2026_ytd else None

    # AI insights
    insights, insights_updated = _get_or_refresh_insights(db, damon_months, anne_months)

    return templates.TemplateResponse("consult_data.html", {
        "request":               request,
        "user":                  user,
        "active_page":           "consult-data",
        # Consultation
        "annual_rows":           annual_rows,
        "monthly_by_year":       monthly_by_year,
        "all_years":             all_consult_years,
        "damon_alltime":         damon_alltime,
        "damon_2026_ytd":        damon_2026_ytd,
        "anne_2026_ytd":         anne_2026_ytd,
        "combined_2026_ytd":     combined_2026_ytd,
        "damon_2025_ytd":        damon_2025_ytd,
        "combined_2025_ytd":     combined_2025_ytd,
        "damon_yoy_pct":         damon_yoy_pct,
        "combined_yoy_pct":      combined_yoy_pct,
        "ytd_period":            ytd_period,
        "damon_2025":            damon_2025,
        "anne_2025":             anne_2025,
        "trend_data":            trend_data,
        # Signing
        "sign_annual_rows":      sign_annual_rows,
        "sign_monthly_by_year":  sign_monthly_by_year,
        "sign_all_years":        all_sign_years,
        "sign_damon_alltime":    sign_damon_alltime,
        "sign_damon_2026_ytd":   sign_damon_2026_ytd,
        "sign_anne_2026_ytd":    sign_anne_2026_ytd,
        "sign_combined_ytd":     sign_combined_ytd,
        "sign_damon_2025_ytd":   sign_damon_2025_ytd,
        "sign_damon_yoy_pct":    sign_damon_yoy_pct,
        "sign_damon_2025":       sign_damon_2025,
        "sign_trend_data":       sign_trend_data,
        "conv_rate_2026":        conv_rate_2026,
        "conv_rate_2025":        conv_rate_2025,
        # Contracts (DocuSign)
        "contract_annual_rows":      contract_annual_rows,
        "contract_monthly_by_year":  contract_monthly_by_year,
        "contracts_alltime":         contracts_alltime,
        "contracts_2026_ytd":        contracts_2026_ytd,
        "contracts_2025":            contracts_2025,
        "contracts_yoy_pct":         contracts_yoy_pct,
        "contract_trend_data":       contract_trend_data,
        "contract_conv_rate_2026":   contract_conv_rate_2026,
        "contract_conv_rate_2025":   contract_conv_rate_2025,
        "contract_to_filed_2026":    contract_to_filed_2026,
        "pacer_filed_ytd":           pacer_filed_ytd,
        "pacer_consult_rate_2026":   pacer_consult_rate_2026,
        # Shared
        "insights":              insights,
        "insights_updated":      insights_updated,
        "notes":                 damon_data.get("notes", []) + anne_data.get("notes", []),
        "updated_at":         damon_data.get("updated_at", ""),
    })
