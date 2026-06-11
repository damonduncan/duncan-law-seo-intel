import threading
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
    "charlotte":     "Charlotte",
    "greensboro":    "Greensboro",
    "winston_salem": "Winston-Salem",
    "salisbury":     "Salisbury",
    "asheville":     "Asheville",
}
MARKETS = list(MARKET_DISPLAY.keys())
MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

MARKET_DISTRICT = {
    "charlotte":     "WDNC",
    "greensboro":    "MDNC",
    "winston_salem": "MDNC",
    "salisbury":     "MDNC",
    "asheville":     "WDNC",
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
    _raw = (cache_row.value or []) if cache_row else []
    # Seed script stores {"months": [...], "source": ...} — unwrap if needed
    ppc_cache: list = _raw.get("months", []) if isinstance(_raw, dict) else _raw

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
            "ga_summary":    None,
            "ga_trend_chart": {"labels": [], "organic": [], "paid": [], "direct": []},
            "total_monthly_spend": "0",
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

    total_monthly_spend = "{:,.0f}".format(
        sum(row["monthly_spend"] or 0 for row in organic_vs_ppc)
    )

    # ── Google Ads setup status ───────────────────────────────────────────────
    try:
        from app.services.google_ads_service import is_configured as _ads_ok
        from app.config import settings as _s
        ads_connected    = _ads_ok()
        ads_has_dev_token = bool(_s.google_ads_developer_token)
        ads_has_customer  = bool(_s.google_ads_customer_id)
    except Exception:
        ads_connected = ads_has_dev_token = ads_has_customer = False

    # Latest data point label
    latest_data_label = months[-1]["label"] if months else None

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
        "total_monthly_spend":  total_monthly_spend,
        "ads_connected":        ads_connected,
        "ads_has_dev_token":    ads_has_dev_token,
        "ads_has_customer":     ads_has_customer,
        "latest_data_label":    latest_data_label,
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
    _raw_ex = (cache_row.value or []) if cache_row else []
    existing: list = _raw_ex.get("months", []) if isinstance(_raw_ex, dict) else _raw_ex

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


# ── Google Ads connect / sync ─────────────────────────────────────────────────

_GOOGLE_ADS_SCOPES = "https://www.googleapis.com/auth/adwords"
_GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
_GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"


@router.get("/ppc/connect-google-ads", response_class=HTMLResponse)
def connect_google_ads(
    request:  Request,
    user:     dict = Depends(auth_required),
    code:     str  = "",
    error:    str  = "",
):
    """Step 1 (GET with no params): show connect page with OAuth URL.
       Step 2 (GET with ?code=...): exchange code for tokens, save refresh token.
    """
    from app.config import settings
    from app.services.google_ads_service import save_refresh_token

    base_url     = settings.app_base_url.rstrip("/")
    redirect_uri = f"{base_url}/ppc/connect-google-ads"
    client_id    = settings.google_client_id
    client_secret = settings.google_client_secret

    message = ""
    success = False

    if error:
        message = f"Google returned an error: {error}"

    elif code:
        # Exchange authorization code for tokens
        try:
            import httpx
            resp = httpx.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "redirect_uri":  redirect_uri,
                },
                timeout=15,
            )
            resp.raise_for_status()
            tokens = resp.json()
            refresh_token = tokens.get("refresh_token", "")
            if refresh_token:
                save_refresh_token(refresh_token)
                message = "Google Ads connected successfully. Refresh token saved."
                success = True
            else:
                message = (
                    "Google returned tokens but no refresh_token. "
                    "Make sure 'offline' access type was requested. "
                    f"Response keys: {list(tokens.keys())}"
                )
        except Exception as e:
            message = f"Token exchange failed: {e}"

    # Build the OAuth URL for the connect button
    from urllib.parse import urlencode
    oauth_params = {
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         _GOOGLE_ADS_SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",
    }
    oauth_url = f"{_GOOGLE_AUTH_URL}?{urlencode(oauth_params)}"

    missing = []
    if not settings.google_ads_developer_token:
        missing.append("GOOGLE_ADS_DEVELOPER_TOKEN")
    if not settings.google_ads_customer_id:
        missing.append("GOOGLE_ADS_CUSTOMER_ID")

    html = f"""
    <!DOCTYPE html><html lang="en"><head>
    <meta charset="UTF-8"><title>Connect Google Ads</title>
    <style>
      body {{font-family:-apple-system,Arial,sans-serif;background:#f3f4f6;padding:40px 24px;}}
      .card {{max-width:600px;margin:0 auto;background:#fff;border-radius:8px;padding:32px;box-shadow:0 1px 3px rgba(0,0,0,.1);}}
      h2 {{margin:0 0 8px;font-size:20px;color:#111827;}}
      p {{color:#374151;font-size:14px;line-height:1.6;}}
      .btn {{display:inline-block;background:#2563eb;color:#fff;padding:10px 22px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:600;margin-top:16px;}}
      .ok {{background:#d1fae5;color:#065f46;padding:12px 16px;border-radius:6px;margin-bottom:16px;font-size:14px;}}
      .err {{background:#fee2e2;color:#991b1b;padding:12px 16px;border-radius:6px;margin-bottom:16px;font-size:14px;}}
      .warn {{background:#fef3c7;color:#78350f;padding:12px 16px;border-radius:6px;margin-bottom:16px;font-size:14px;}}
      code {{background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:13px;}}
      ol {{color:#374151;font-size:14px;line-height:1.8;padding-left:20px;}}
    </style>
    </head><body>
    <div class="card">
      <h2>Connect Google Ads</h2>
      <p>Authorize Market Pulse to read your Google Ads performance data. This is a one-time setup — once connected, monthly PPC data will sync automatically.</p>

      {"<div class='ok'>✓ " + message + "</div>" if success else ""}
      {"<div class='err'>✗ " + message + "</div>" if message and not success else ""}

      {"<div class='warn'>⚠ Missing Railway env vars: <strong>" + ", ".join(missing) + "</strong>. Add these before connecting.</div>" if missing else ""}

      <ol>
        <li>In Railway, set <code>GOOGLE_ADS_DEVELOPER_TOKEN</code> and <code>GOOGLE_ADS_CUSTOMER_ID</code></li>
        <li>Add <code>{redirect_uri}</code> to your Google Cloud Console OAuth authorized redirect URIs</li>
        <li>Click the button below and authorize access to Google Ads</li>
        <li>You'll be redirected back here with a success message</li>
      </ol>

      {"" if success else f'<a href="{oauth_url}" class="btn">Authorize Google Ads →</a>'}
      {"<br><a href='/ppc' class='btn' style='background:#059669;margin-top:12px;'>Back to Lead Intelligence →</a>" if success else ""}
      {"<br><br><a href='/ppc' style='font-size:13px;color:#6b7280;'>← Back to Lead Intelligence</a>" if not success else ""}
    </div>
    </body></html>
    """
    return HTMLResponse(content=html)


def _run_google_ads_sync(months_back: int = 3) -> dict:
    """Sync the last N months of Google Ads data into ppc_monthly_data cache."""
    from datetime import date
    from app.jobs.monthly import _upsert_ppc_month
    from app.services.google_ads_service import fetch_ppc_monthly
    from app.database import SessionLocal

    today  = date.today()
    synced = []
    errors = []

    db = SessionLocal()
    try:
        for offset in range(1, months_back + 1):
            year  = today.year  if today.month > offset else today.year - 1
            month = (today.month - offset - 1) % 12 + 1
            # Simpler: subtract offset months from today
            from datetime import date as d_
            ref   = date(today.year, today.month, 1)
            mo    = ref.month - offset
            yr    = ref.year
            while mo <= 0:
                mo += 12
                yr -= 1
            try:
                entry = fetch_ppc_monthly(yr, mo)
                _upsert_ppc_month(db, entry)
                synced.append(f"{yr}-{mo:02d}")
            except Exception as e:
                errors.append(f"{yr}-{mo:02d}: {e}")
    finally:
        db.close()

    return {"synced": synced, "errors": errors}


@router.post("/ppc/sync-google-ads")
def sync_google_ads_now(
    request: Request,
    user:    dict = Depends(auth_required),
    months:  int  = Form(3),
):
    """Manually trigger a Google Ads data sync for the last N months."""
    from app.services.google_ads_service import is_configured

    if not is_configured():
        return RedirectResponse(url="/ppc/connect-google-ads", status_code=303)

    def _bg():
        result = _run_google_ads_sync(months_back=min(months, 24))
        import logging
        logging.getLogger(__name__).info(f"Google Ads manual sync: {result}")

    threading.Thread(target=_bg, daemon=True).start()
    return RedirectResponse(url="/ppc?msg=ads_sync_started", status_code=303)
