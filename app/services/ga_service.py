import json
import logging
import calendar
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# City name (lowercase) → internal market key
CITY_MARKET_MAP = {
    "charlotte":       "charlotte",
    "concord":         "charlotte",
    "gastonia":        "charlotte",
    "rock hill":       "charlotte",
    "huntersville":    "charlotte",
    "matthews":        "charlotte",
    "mint hill":       "charlotte",
    "cornelius":       "charlotte",
    "davidson":        "charlotte",
    "mooresville":     "charlotte",
    "belmont":         "charlotte",
    "greensboro":      "greensboro",
    "high point":      "greensboro",
    "burlington":      "greensboro",
    "mebane":          "greensboro",
    "asheboro":        "greensboro",
    "winston-salem":   "winston_salem",
    "winston salem":   "winston_salem",
    "kernersville":    "winston_salem",
    "clemmons":        "winston_salem",
    "lewisville":      "winston_salem",
    "salisbury":       "salisbury",
    "kannapolis":      "salisbury",
    "china grove":     "salisbury",
    "landis":          "salisbury",
    "rockwell":        "salisbury",
    "spencer":         "salisbury",
}

KNOWN_MARKETS = ["charlotte", "greensboro", "winston_salem", "salisbury"]


def _get_client():
    """Create GA4 Data API client from service account JSON string in env."""
    from app.config import settings
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account

    raw = settings.ga_credentials_json
    if not raw:
        raise ValueError("GA_CREDENTIALS_JSON is not configured")

    creds_dict = json.loads(raw)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    return BetaAnalyticsDataClient(credentials=credentials)


def fetch_monthly_traffic(year: int, month: int) -> dict:
    """
    Pull GA4 data for a full calendar month.
    Returns channel breakdown + per-market organic/paid session counts.
    """
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest,
    )
    from app.config import settings

    start   = date(year, month, 1)
    end     = date(year, month, calendar.monthrange(year, month)[1])
    prop    = f"properties/{settings.ga_property_id}"
    client  = _get_client()

    # ── Query 1: Channel breakdown (sessions, new users, key events) ──────────
    ch_resp = client.run_report(RunReportRequest(
        property=prop,
        dimensions=[Dimension(name="sessionDefaultChannelGroup")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="newUsers"),
            Metric(name="keyEvents"),
        ],
        date_ranges=[DateRange(start_date=str(start), end_date=str(end))],
    ))

    channels = {}
    for row in ch_resp.rows:
        ch = row.dimension_values[0].value
        channels[ch] = {
            "sessions":    int(row.metric_values[0].value or 0),
            "new_users":   int(row.metric_values[1].value or 0),
            "conversions": int(row.metric_values[2].value or 0),
        }

    # ── Query 2: City × channel (to map sessions to markets) ─────────────────
    city_resp = client.run_report(RunReportRequest(
        property=prop,
        dimensions=[
            Dimension(name="city"),
            Dimension(name="sessionDefaultChannelGroup"),
        ],
        metrics=[
            Metric(name="sessions"),
            Metric(name="keyEvents"),
        ],
        date_ranges=[DateRange(start_date=str(start), end_date=str(end))],
    ))

    markets = {m: {
        "organic_sessions": 0,
        "paid_sessions":    0,
        "total_sessions":   0,
        "conversions":      0,
    } for m in KNOWN_MARKETS}

    for row in city_resp.rows:
        city_raw = row.dimension_values[0].value.lower().strip()
        channel  = row.dimension_values[1].value
        sessions = int(row.metric_values[0].value or 0)
        convs    = int(row.metric_values[1].value or 0)

        mkt = CITY_MARKET_MAP.get(city_raw)
        if mkt is None:
            continue

        markets[mkt]["total_sessions"] += sessions
        markets[mkt]["conversions"]    += convs
        if "Organic" in channel:
            markets[mkt]["organic_sessions"] += sessions
        elif "Paid" in channel:
            markets[mkt]["paid_sessions"] += sessions

    total_sessions    = sum(v["sessions"] for v in channels.values())
    total_conversions = sum(v["conversions"] for v in channels.values())

    return {
        "year":              year,
        "month":             month,
        "channels":          channels,
        "markets":           markets,
        "total_sessions":    total_sessions,
        "total_organic":     channels.get("Organic Search", {}).get("sessions", 0),
        "total_paid":        channels.get("Paid Search", {}).get("sessions", 0),
        "total_direct":      channels.get("Direct", {}).get("sessions", 0),
        "total_conversions": total_conversions,
        "fetched_at":        datetime.now(timezone.utc).isoformat(),
    }


def run_ga_pull(db) -> int:
    """
    Pull the last 3 complete calendar months of GA4 data.
    Skips months already cached. Returns count of months newly fetched.
    Called from the monthly scheduler job.
    """
    from app.models.discovery import DiscoveryCache
    from app.models.base import new_uuid

    today     = date.today()
    cache_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "ga_monthly_data"
    ).first()
    existing: list = list(cache_row.value or []) if cache_row else []
    have = {(m["year"], m["month"]) for m in existing}

    # Build list of last 3 complete months (not the current partial month)
    targets = []
    y, mo = today.year, today.month
    for _ in range(3):
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1
        targets.append((y, mo))

    fetched = 0
    for (y, mo) in targets:
        if (y, mo) in have:
            continue
        try:
            data = fetch_monthly_traffic(y, mo)
            existing.append(data)
            have.add((y, mo))
            fetched += 1
            logger.info(
                f"GA4 pulled {y}-{mo:02d}: "
                f"{data['total_sessions']} sessions, "
                f"{data['total_organic']} organic"
            )
        except Exception as e:
            logger.error(f"GA4 pull failed for {y}-{mo:02d}: {e}", exc_info=True)

    if fetched:
        existing.sort(key=lambda m: (m["year"], m["month"]))
        if cache_row:
            cache_row.value      = existing
            cache_row.updated_at = datetime.now(timezone.utc)
        else:
            db.add(DiscoveryCache(
                id=new_uuid(),
                key="ga_monthly_data",
                value=existing,
                updated_at=datetime.now(timezone.utc),
            ))
        db.commit()

    return fetched


def get_ga_monthly_data(db) -> list:
    """Return list of GA monthly data dicts, sorted oldest→newest."""
    from app.models.discovery import DiscoveryCache
    row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "ga_monthly_data"
    ).first()
    if not row or not row.value:
        return []
    return sorted(row.value, key=lambda m: (m["year"], m["month"]))
