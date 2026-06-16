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
from app.routers.formstack import load_referral_sources

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()
logger = logging.getLogger(__name__)

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Conservative blended average fee per case (Ch.7 $1,995 fixed; Ch.13 variable/dismissals pull it down)
AVG_CASE_FEE = 1_500


def _fmt_revenue(n: int) -> str:
    """Format an integer dollar amount as $XK or $X.XM."""
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${round(n / 1_000)}K"
    return f"${n:,}"


def _yoy_pct(current: int, prior: int):
    if prior == 0:
        return None
    return round((current - prior) / prior * 100, 1)


def _generate_insights(damon_months: dict, anne_months: dict,
                        funnel_data: dict = None) -> list:
    """Call Claude to produce 5 analytical insights from consultation and funnel data."""
    from app.config import settings
    import anthropic

    if not settings.anthropic_api_key:
        return []

    all_years = sorted(set(y for (y, _) in list(damon_months.keys()) + list(anne_months.keys())))

    annual_lines = []
    for year in all_years:
        d = sum(damon_months.get((year, m), 0) for m in range(1, 13))
        a = sum(anne_months.get((year, m), 0)  for m in range(1, 13))
        annual_lines.append(f"  {year}: Damon={d}, Anne={a}, Total={d + a}")

    full_years = [y for y in range(2010, 2026) if sum(damon_months.get((y, m), 0) for m in range(1, 13)) > 0]
    season_lines = []
    for m in range(1, 13):
        vals = [damon_months.get((y, m), 0) for y in full_years if damon_months.get((y, m)) is not None]
        if vals:
            avg = sum(vals) / len(vals)
            season_lines.append(f"  {MONTH_NAMES[m - 1]}: avg {avg:.1f} ({len(vals)} yrs of data)")

    last_month  = funnel_data.get("last_month", 5) if funnel_data else 5
    ytd_label   = funnel_data.get("ytd_period", "Jan–May") if funnel_data else "Jan–May"
    d_2026 = sum(damon_months.get((2026, m), 0) for m in range(1, last_month + 1))
    d_2025 = sum(damon_months.get((2025, m), 0) for m in range(1, last_month + 1))
    a_2026 = sum(anne_months.get((2026, m), 0)  for m in range(1, last_month + 1))

    anne_recent = sorted([(y, m, anne_months[(y, m)]) for (y, m) in anne_months], key=lambda x: (x[0], x[1]))
    anne_lines  = [f"  {MONTH_NAMES[m - 1]} {y}: {c}" for y, m, c in anne_recent]

    funnel_section = ""
    if funnel_data:
        fd = funnel_data
        funnel_section = f"""
INTAKE FUNNEL — {ytd_label} 2026 vs same period 2025:
  Consultations:       2026={fd.get('combined_2026_ytd','?')}, 2025={fd.get('combined_2025_ytd','?')} (Damon only in 2025)
  Contracts Signed:    2026={fd.get('contracts_2026_ytd','?')}, 2025={fd.get('contracts_2025_ytd','?')}
    Consult→Contract:  2026={fd.get('contract_conv_rate_2026','?')}%, 2025={fd.get('contract_conv_rate_2025_ytd','?')}%
  Cases Filed (PACER): 2026={fd.get('pacer_filed_ytd','?')}, 2025={fd.get('pacer_2025_ytd','?')}
    Contract→Filed:    2026={fd.get('contract_to_filed_2026','?')}%, 2025={fd.get('contract_to_filed_2025','?')}%
    Overall consult→filed: 2026={fd.get('pacer_consult_rate_2026','?')}%, 2025={fd.get('pacer_consult_rate_2025','?')}%
  Est. Revenue (@ ${fd.get('avg_case_fee','?')}/case avg): 2026={fd.get('est_revenue_2026_ytd','?')}, 2025={fd.get('est_revenue_2025_ytd','?')}
"""

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
- Contracts Signed = attorney-client agreements via DocuSign (prospect becomes a paying client).
- Cases Filed = bankruptcy petitions filed with the court, sourced from PACER.

Analyze the data below and return exactly 5 concise, specific, actionable insights useful to the
firm's principals — covering intake trends, funnel conversion rates, seasonality, Anne's ramp
trajectory, and forecasts. Each insight must reference specific numbers.
Write for attorneys, not data scientists. Do not flag Damon's individual decline as a concern.

ANNUAL CONSULTATION TOTALS:
{chr(10).join(annual_lines)}

DAMON'S HISTORICAL AVERAGE BY CALENDAR MONTH (2010–2025 full years only):
{chr(10).join(season_lines)}

{ytd_label} 2026 YTD:  Damon={d_2026}, Anne={a_2026}, Combined={d_2026 + a_2026}
{ytd_label} 2025 YTD:  Damon={d_2025} (Anne not yet hired)

ANNE'S MONTHLY COUNTS SINCE JOINING:
{chr(10).join(anne_lines)}
{funnel_section}
Return ONLY a valid JSON array (no markdown, no explanation) with 5 objects, each having:
  "category": one of trend | seasonality | capacity | forecast | funnel
  "text": 1–2 sentences, specific numbers, actionable tone
  "sentiment": positive | negative | neutral"""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
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
                              force: bool = False,
                              funnel_data: dict = None) -> tuple[list, str]:
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
        insights = _generate_insights(damon_months, anne_months, funnel_data)
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


def _generate_funnel_snapshot(funnel_data: dict) -> str:
    """Call Claude to produce a 3–4 sentence funnel summary paragraph."""
    from app.config import settings
    import anthropic

    if not settings.anthropic_api_key or not funnel_data:
        return ""

    fd = funnel_data
    prompt = f"""You are a practice management analyst for Duncan Law LLP, a consumer bankruptcy firm in Charlotte, NC.
Damon Duncan is the founding attorney; Anne Salter joined in October 2025 to handle consultations so Damon can focus on signing appointments and filings.

CRITICAL CONTEXT:
- Damon deliberately shifted FROM doing consultations TO doing signing appointments as Anne ramped up.
  His declining individual consult count is a sign of healthy delegation, NOT a problem.
- The right measure of intake health is COMBINED consultation volume (Damon + Anne).
- Contracts Signed = attorney-client agreements via DocuSign (prospect becomes a paying client).
- Cases Filed = bankruptcy petitions filed with the court, sourced from PACER.
- Est. revenue uses a conservative blended average of ${fd.get('avg_case_fee', 1500)}/case; it is not actual billed revenue.

INTAKE FUNNEL DATA — {fd.get('ytd_period', '')} 2026 vs same period 2025:
  Consultations:       2026={fd.get('combined_2026_ytd','?')}, 2025={fd.get('combined_2025_ytd','?')}
  Contracts Signed:    2026={fd.get('contracts_2026_ytd','?')}, 2025={fd.get('contracts_2025_ytd','?')}
    Consult→Contract:  2026={fd.get('contract_conv_rate_2026','?')}%, 2025={fd.get('contract_conv_rate_2025_ytd','?')}%
  Cases Filed (PACER): 2026={fd.get('pacer_filed_ytd','?')}, 2025={fd.get('pacer_2025_ytd','?')}
    Contract→Filed:    2026={fd.get('contract_to_filed_2026','?')}%, 2025={fd.get('contract_to_filed_2025','?')}%
    Overall rate:      2026={fd.get('pacer_consult_rate_2026','?')}%, 2025={fd.get('pacer_consult_rate_2025','?')}%
  Est. Revenue:        2026={fd.get('est_revenue_2026_ytd','?')}, 2025={fd.get('est_revenue_2025_ytd','?')}

Write exactly 3–4 sentences that give Damon a clear, honest picture of intake funnel health right now.
Reference specific numbers. Highlight what's working, flag any meaningful conversion gap worth watching,
and close with one forward-looking observation or recommendation.
Do NOT mention Damon's individual consult decline as a concern. Write in plain, direct prose — no bullet points,
no headers, no markdown. Output only the paragraph, nothing else."""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _get_or_refresh_funnel_snapshot(db: Session, funnel_data: dict) -> tuple[str, str]:
    """Return (snapshot_text, updated_label). Regenerates if stale (>7 days)."""
    row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "funnel_snapshot"
    ).first()

    if row and row.updated_at:
        age = datetime.now(timezone.utc) - row.updated_at.replace(tzinfo=timezone.utc)
        if age < timedelta(days=7):
            text = row.value if isinstance(row.value, str) else (row.value or {}).get("text", "")
            return text, row.updated_at.strftime("%b %d, %Y")

    try:
        text = _generate_funnel_snapshot(funnel_data)
        now = datetime.now(timezone.utc)
        if row:
            row.value      = text
            row.updated_at = now
        else:
            db.add(DiscoveryCache(id=new_uuid(), key="funnel_snapshot",
                                  value=text, updated_at=now))
        db.commit()
        return text, now.strftime("%b %d, %Y")
    except Exception as e:
        logger.error(f"Failed to generate funnel snapshot: {e}", exc_info=True)
        if row:
            text = row.value if isinstance(row.value, str) else ""
            return text, row.updated_at.strftime("%b %d, %Y") if row.updated_at else ""
        return "", ""


def _bg_refresh_insights() -> None:
    """Background task — loads all data from its own DB session."""
    import json as _json
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        def _load_bg(key):
            row = db.query(DiscoveryCache).filter(DiscoveryCache.key == key).first()
            data = row.value if row else {}
            if isinstance(data, str):
                data = _json.loads(data)
            return {(m["year"], m["month"]): m["count"] for m in data.get("months", [])}

        damon_months    = _load_bg("consultation_monthly_damon")
        anne_months     = _load_bg("consultation_monthly_anne")
        contract_months = _load_bg("docusign_monthly_contracts")

        m2026      = [m for (y, m) in list(damon_months) + list(anne_months) if y == 2026]
        last_month = max(m2026) if m2026 else 5
        ytd_period = f"Jan–{MONTH_NAMES[last_month - 1]}"

        _fr = db.query(DiscoveryCache).filter(DiscoveryCache.key == "duncan_law_filing_history").first()
        _fh = _fr.value if _fr else {}
        if isinstance(_fh, str):
            _fh = _json.loads(_fh)

        def _ytd(months, year):
            return sum(months.get((year, m), 0) for m in range(1, last_month + 1))

        def _pacer_ytd(fh, year):
            annual = next((r for r in fh.get("annual", []) if r["year"] == year), None)
            if not annual:
                return 0
            monthly = annual.get("monthly") or []
            return sum(monthly[m - 1] or 0 for m in range(1, last_month + 1) if m - 1 < len(monthly))

        def _pct(num, den):
            return round(num / den * 100, 1) if den else None

        c2026 = _ytd(damon_months, 2026) + _ytd(anne_months, 2026)
        c2025 = _ytd(damon_months, 2025)
        k2026 = _ytd(contract_months, 2026)
        k2025 = _ytd(contract_months, 2025)
        p2026 = _pacer_ytd(_fh, 2026)
        p2025 = _pacer_ytd(_fh, 2025)

        funnel_data = {
            "last_month": last_month, "ytd_period": ytd_period,
            "combined_2026_ytd": c2026,   "combined_2025_ytd": c2025,
            "contracts_2026_ytd": k2026,  "contracts_2025_ytd": k2025,
            "pacer_filed_ytd": p2026,     "pacer_2025_ytd": p2025,
            "contract_conv_rate_2026": _pct(k2026, c2026),
            "contract_conv_rate_2025_ytd": _pct(k2025, c2025),
            "contract_to_filed_2026": _pct(p2026, k2026),
            "contract_to_filed_2025": _pct(p2025, k2025),
            "pacer_consult_rate_2026": _pct(p2026, c2026),
            "pacer_consult_rate_2025": _pct(p2025, c2025),
        }

        _get_or_refresh_insights(db, damon_months, anne_months, force=True, funnel_data=funnel_data)
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
    background_tasks.add_task(_bg_refresh_insights)
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


def _build_conversion_rate_trend(
    d_months: dict, a_months: dict, c_months: dict,
    filing_hist: dict, last_year: int, last_month: int, n: int = 18,
) -> list:
    """Return monthly conversion rates (rolling 3-month sums) for the last N months."""
    filing_map: dict = {}
    for annual in filing_hist.get("annual", []):
        year = annual["year"]
        for i, cnt in enumerate(annual.get("monthly") or []):
            if cnt is not None:
                filing_map[(year, i + 1)] = cnt

    ref_total = last_year * 12 + (last_month - 1)
    rows = []
    for offset in range(n - 1, -1, -1):
        t = ref_total - offset
        y, rem = divmod(t, 12)
        m = rem + 1
        consult  = (d_months.get((y, m), 0) or 0) + (a_months.get((y, m), 0) or 0)
        contract = c_months.get((y, m), 0) or 0
        filed    = filing_map.get((y, m), 0) or 0
        rows.append({
            "label":    f"{MONTH_NAMES[m - 1]} '{str(y)[-2:]}",
            "consult":  consult,
            "contract": contract,
            "filed":    filed,
        })

    for i, row in enumerate(rows):
        window     = rows[max(0, i - 2): i + 1]
        w_consult  = sum(w["consult"]  for w in window)
        w_contract = sum(w["contract"] for w in window)
        w_filed    = sum(w["filed"]    for w in window)
        row["contract_rate_r3"] = round(w_contract / w_consult  * 100, 1) if w_consult  else None
        row["filed_rate_r3"]    = round(w_filed    / w_contract * 100, 1) if w_contract else None
        row["overall_rate_r3"]  = round(w_filed    / w_consult  * 100, 1) if w_consult  else None

    return rows


def _build_pacer_trend(filing_hist: dict, last_year: int, last_month: int, n: int = 24):
    monthly_map = {}
    for annual in filing_hist.get("annual", []):
        year = annual["year"]
        for i, cnt in enumerate(annual.get("monthly") or []):
            if cnt is not None:
                monthly_map[(year, i + 1)] = cnt
    ref_total = last_year * 12 + (last_month - 1)
    data = []
    for offset in range(n - 1, -1, -1):
        t = ref_total - offset
        y, rem = divmod(t, 12)
        m = rem + 1
        data.append({"label": f"{MONTH_NAMES[m - 1]} '{str(y)[-2:]}",
                     "count": monthly_map.get((y, m), 0)})
    return data


def _get_current_month_pacing(db, last_complete_month: int,
                               damon_months: dict, anne_months: dict) -> dict | None:
    """Return current-month consultation + signing counts with a 2-hour cache."""
    import calendar as cal_mod
    from datetime import date
    today      = date.today()
    curr_month = last_complete_month + 1
    curr_year  = 2026
    if curr_month > 12:
        return None

    import json as _json
    # v2 cache key includes signing appointments
    cache_key = f"pacing_v2_{curr_year}_{curr_month:02d}"
    cache_row = db.query(DiscoveryCache).filter(DiscoveryCache.key == cache_key).first()

    if cache_row and cache_row.updated_at:
        age = datetime.now(timezone.utc) - cache_row.updated_at.replace(tzinfo=timezone.utc)
        if age < timedelta(hours=2):
            data = cache_row.value
            if isinstance(data, str):
                data = _json.loads(data)
            return data

    try:
        from app.services.calendar_service import fetch_month_count
        damon_count  = fetch_month_count(email="damonduncan@duncanlawonline.com",
                                          attorney="damon", year=curr_year, month=curr_month,
                                          event_type="consult")
        anne_count   = fetch_month_count(email="anne@duncanlawonline.com",
                                          attorney="anne",  year=curr_year, month=curr_month,
                                          event_type="consult")
        damon_sign   = fetch_month_count(email="damonduncan@duncanlawonline.com",
                                          attorney="damon", year=curr_year, month=curr_month,
                                          event_type="signing")
        anne_sign    = fetch_month_count(email="anne@duncanlawonline.com",
                                          attorney="anne",  year=curr_year, month=curr_month,
                                          event_type="signing")

        days_in_month = cal_mod.monthrange(curr_year, curr_month)[1]
        day_of_month  = min(today.day, days_in_month)
        combined      = damon_count + anne_count
        pace_full     = round(combined / day_of_month * days_in_month) if day_of_month else None
        prior_damon   = damon_months.get((curr_year - 1, curr_month), 0)
        prior_anne    = anne_months.get((curr_year - 1, curr_month), 0)

        data = {
            "year": curr_year, "month": curr_month,
            "month_name": MONTH_NAMES[curr_month - 1],
            "damon": damon_count, "anne": anne_count, "combined": combined,
            "sign_damon": damon_sign, "sign_anne": anne_sign,
            "sign_combined": damon_sign + anne_sign,
            "day_of_month": day_of_month, "days_in_month": days_in_month,
            "pace_full": pace_full,
            "prior_combined": prior_damon + prior_anne,
            "prior_year": curr_year - 1,
        }
        now = datetime.now(timezone.utc)
        if cache_row:
            cache_row.value      = data
            cache_row.updated_at = now
        else:
            db.add(DiscoveryCache(id=new_uuid(), key=cache_key, value=data, updated_at=now))
        db.commit()
        return data
    except Exception as e:
        logger.error(f"Current month pacing fetch failed: {e}", exc_info=True)
        if cache_row:
            data = cache_row.value
            if isinstance(data, str):
                data = _json.loads(data)
            return data
        return None


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

    # ── PACER 2025 YTD (same period, for funnel comparison row) ──────────────
    _pacer_2025 = next((r for r in _filing_hist.get("annual", []) if r["year"] == 2025), None)
    pacer_2025_ytd = 0
    if _pacer_2025:
        _monthly_2025 = _pacer_2025.get("monthly") or []
        pacer_2025_ytd = sum(
            _monthly_2025[m - 1] or 0
            for m in range(1, last_2026_month + 1)
            if m - 1 < len(_monthly_2025)
        )
    contract_conv_rate_2025_ytd = round(contracts_2025_ytd / combined_2025_ytd * 100, 1) if combined_2025_ytd else None
    contract_to_filed_2025      = round(pacer_2025_ytd / contracts_2025_ytd * 100, 1) if contracts_2025_ytd else None
    pacer_consult_rate_2025     = round(pacer_2025_ytd / combined_2025_ytd * 100, 1) if combined_2025_ytd else None

    # ── Estimated revenue (conservative blended avg $1,500/case) ─────────────
    est_revenue_2026_ytd = _fmt_revenue(pacer_filed_ytd * AVG_CASE_FEE)
    est_revenue_2025_ytd = _fmt_revenue(pacer_2025_ytd * AVG_CASE_FEE)

    # ── PACER 24-month trend ──────────────────────────────────────────────────
    pacer_trend_data = _build_pacer_trend(_filing_hist, 2026, last_2026_month)
    for _item in pacer_trend_data:
        _item["revenue"] = _item["count"] * AVG_CASE_FEE

    conv_rate_trend = _build_conversion_rate_trend(
        damon_months, anne_months, contract_months, _filing_hist, 2026, last_2026_month
    )

    # Annual pace projection
    filings_monthly_avg    = pacer_filed_ytd / last_2026_month if last_2026_month else 0
    filings_full_year_pace = round(filings_monthly_avg * 12)
    est_revenue_annual_pace = _fmt_revenue(filings_full_year_pace * AVG_CASE_FEE)

    # ── Current month pacing ──────────────────────────────────────────────────
    current_month_pacing = _get_current_month_pacing(db, last_2026_month, damon_months, anne_months)

    # AI insights — pass full funnel context so the model can reference conversion rates
    funnel_data = {
        "last_month": last_2026_month, "ytd_period": ytd_period,
        "combined_2026_ytd": combined_2026_ytd,  "combined_2025_ytd": combined_2025_ytd,
        "contracts_2026_ytd": contracts_2026_ytd, "contracts_2025_ytd": contracts_2025_ytd,
        "pacer_filed_ytd": pacer_filed_ytd,       "pacer_2025_ytd": pacer_2025_ytd,
        "contract_conv_rate_2026": contract_conv_rate_2026,
        "contract_conv_rate_2025_ytd": contract_conv_rate_2025_ytd,
        "contract_to_filed_2026": contract_to_filed_2026,
        "contract_to_filed_2025": contract_to_filed_2025,
        "pacer_consult_rate_2026": pacer_consult_rate_2026,
        "pacer_consult_rate_2025": pacer_consult_rate_2025,
        "est_revenue_2026_ytd": est_revenue_2026_ytd,
        "est_revenue_2025_ytd": est_revenue_2025_ytd,
        "avg_case_fee": AVG_CASE_FEE,
    }
    insights, insights_updated = _get_or_refresh_insights(
        db, damon_months, anne_months, funnel_data=funnel_data
    )
    funnel_snapshot, funnel_snapshot_updated = _get_or_refresh_funnel_snapshot(db, funnel_data)
    referral_sources = load_referral_sources(db)

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
        "contracts_2025_ytd":        contracts_2025_ytd,
        "contracts_2025":            contracts_2025,
        "contracts_yoy_pct":         contracts_yoy_pct,
        "contract_trend_data":       contract_trend_data,
        "contract_conv_rate_2026":   contract_conv_rate_2026,
        "contract_conv_rate_2025":   contract_conv_rate_2025,
        "contract_to_filed_2026":    contract_to_filed_2026,
        "pacer_filed_ytd":           pacer_filed_ytd,
        "pacer_consult_rate_2026":   pacer_consult_rate_2026,
        # PACER 2025 comparison
        "pacer_2025_ytd":            pacer_2025_ytd,
        "contract_conv_rate_2025_ytd": contract_conv_rate_2025_ytd,
        "contract_to_filed_2025":    contract_to_filed_2025,
        "pacer_consult_rate_2025":   pacer_consult_rate_2025,
        # PACER trend & pacing
        "pacer_trend_data":          pacer_trend_data,
        "current_month_pacing":      current_month_pacing,
        "est_revenue_2026_ytd":      est_revenue_2026_ytd,
        "est_revenue_2025_ytd":      est_revenue_2025_ytd,
        "avg_case_fee":              AVG_CASE_FEE,
        "filings_full_year_pace":    filings_full_year_pace,
        "est_revenue_annual_pace":   est_revenue_annual_pace,
        # Shared
        "funnel_snapshot":       funnel_snapshot,
        "funnel_snapshot_updated": funnel_snapshot_updated,
        "insights":              insights,
        "insights_updated":      insights_updated,
        "notes":                 damon_data.get("notes", []) + anne_data.get("notes", []),
        "updated_at":         damon_data.get("updated_at", ""),
        "referral_sources":      referral_sources,
        "conv_rate_trend":       conv_rate_trend,
    })
