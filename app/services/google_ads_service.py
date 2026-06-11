"""Google Ads API service — pulls monthly PPC metrics via REST/GAQL.

Required env vars (set in Railway):
  GOOGLE_ADS_DEVELOPER_TOKEN  — from Google Ads → Tools → API Center
  GOOGLE_ADS_CUSTOMER_ID      — 10-digit Google Ads account ID (hyphens OK)
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET — already set for login
  Refresh token is stored in discovery_cache key 'google_ads_credentials'
  after completing the one-time auth flow at /admin/google-ads/connect.
"""
import logging
from calendar import monthrange
from typing import Optional

logger = logging.getLogger(__name__)

_API_VERSION = "v17"

# Substring patterns (lowercase) used to map campaign names → market keys
_MARKET_PATTERNS = {
    "charlotte":     ["charlotte"],
    "greensboro":    ["greensboro"],
    "winston_salem": ["winston-salem", "winston salem", "winstonsalem", "winston"],
    "salisbury":     ["salisbury"],
}


def _detect_market(campaign_name: str) -> Optional[str]:
    lower = campaign_name.lower()
    for market, patterns in _MARKET_PATTERNS.items():
        if any(p in lower for p in patterns):
            return market
    return None


def _get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    import httpx
    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def load_refresh_token() -> Optional[str]:
    """Load the Google Ads refresh token from discovery_cache."""
    try:
        from app.database import SessionLocal
        from app.models.discovery import DiscoveryCache
        db = SessionLocal()
        try:
            row = db.query(DiscoveryCache).filter(
                DiscoveryCache.key == "google_ads_credentials"
            ).first()
            if row and isinstance(row.value, dict):
                return row.value.get("refresh_token")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Google Ads: could not load refresh token: {e}")
    return None


def save_refresh_token(refresh_token: str, access_token: str = "") -> None:
    """Persist Google Ads refresh token to discovery_cache."""
    from datetime import datetime, timezone
    from app.database import SessionLocal
    from app.models.discovery import DiscoveryCache
    from app.models.base import new_uuid

    db = SessionLocal()
    try:
        row = db.query(DiscoveryCache).filter(
            DiscoveryCache.key == "google_ads_credentials"
        ).first()
        payload = {
            "refresh_token": refresh_token,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        if row:
            row.value = payload
            row.updated_at = datetime.now(timezone.utc)
        else:
            db.add(DiscoveryCache(
                id=new_uuid(),
                key="google_ads_credentials",
                value=payload,
                updated_at=datetime.now(timezone.utc),
            ))
        db.commit()
    finally:
        db.close()


def is_configured() -> bool:
    """Return True if all required Google Ads credentials are present."""
    from app.config import settings
    has_creds = bool(
        settings.google_ads_developer_token
        and settings.google_ads_customer_id
        and settings.google_client_id
        and settings.google_client_secret
    )
    if not has_creds:
        return False
    return bool(load_refresh_token())


def fetch_ppc_monthly(year: int, month: int) -> dict:
    """Fetch Google Ads metrics for a calendar month and return a per-market dict.

    Return shape matches the ppc_monthly_data cache format:
    {
      "year": 2024, "month": 6,
      "charlotte":     {"impressions": N, "clicks": N, "ctr": N, "spend": N, "leads": N, "cpl": N},
      "greensboro":    {...},
      "winston_salem": {...},
      "salisbury":     {...},
      "total":         {...},
    }
    """
    from app.config import settings
    import httpx

    if not settings.google_ads_developer_token:
        raise ValueError("GOOGLE_ADS_DEVELOPER_TOKEN not configured")
    if not settings.google_ads_customer_id:
        raise ValueError("GOOGLE_ADS_CUSTOMER_ID not configured")

    refresh_token = load_refresh_token()
    if not refresh_token:
        raise ValueError("Google Ads refresh token not found — complete setup at /admin/google-ads/connect")

    last_day   = monthrange(year, month)[1]
    start_date = f"{year}-{month:02d}-01"
    end_date   = f"{year}-{month:02d}-{last_day:02d}"

    access_token = _get_access_token(
        settings.google_client_id,
        settings.google_client_secret,
        refresh_token,
    )

    customer_id = settings.google_ads_customer_id.replace("-", "")

    query = (
        "SELECT campaign.name, campaign.id, "
        "metrics.impressions, metrics.clicks, "
        "metrics.cost_micros, metrics.conversions "
        "FROM campaign "
        f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
        "AND campaign.status != 'REMOVED'"
    )

    resp = httpx.post(
        f"https://googleads.googleapis.com/{_API_VERSION}/customers/{customer_id}/googleAds:search",
        headers={
            "Authorization":   f"Bearer {access_token}",
            "developer-token": settings.google_ads_developer_token,
            "Content-Type":    "application/json",
        },
        json={"query": query},
        timeout=30,
    )
    resp.raise_for_status()

    rows = resp.json().get("results", [])

    market_totals: dict = {}
    unrecognized: list = []
    for row in rows:
        name   = row.get("campaign", {}).get("name", "")
        market = _detect_market(name)
        if not market:
            unrecognized.append(name)
            continue
        m = row.get("metrics", {})
        if market not in market_totals:
            market_totals[market] = {"impressions": 0, "clicks": 0, "spend": 0.0, "leads": 0.0}
        market_totals[market]["impressions"] += int(m.get("impressions", 0))
        market_totals[market]["clicks"]      += int(m.get("clicks", 0))
        market_totals[market]["spend"]       += int(m.get("costMicros", 0)) / 1_000_000
        market_totals[market]["leads"]       += float(m.get("conversions", 0.0))

    if unrecognized:
        unique = list(dict.fromkeys(unrecognized))
        logger.warning(
            f"Google Ads: {len(unique)} campaign name(s) not mapped to a market: "
            + ", ".join(f'"{n}"' for n in unique[:8])
        )

    result = {"year": year, "month": month}
    total_imp = total_clk = 0
    total_sp  = total_lds = 0.0

    for market, data in market_totals.items():
        imp = data["impressions"]
        clk = data["clicks"]
        sp  = data["spend"]
        lds = round(data["leads"])
        result[market] = {
            "impressions": imp,
            "clicks":      clk,
            "ctr":         round(clk / imp * 100, 2) if imp else 0.0,
            "spend":       round(sp, 2),
            "leads":       lds,
            "cpl":         round(sp / lds, 2) if lds else 0.0,
        }
        total_imp += imp
        total_clk += clk
        total_sp  += sp
        total_lds += lds

    total_lds_int = round(total_lds)
    result["total"] = {
        "impressions": total_imp,
        "clicks":      total_clk,
        "ctr":         round(total_clk / total_imp * 100, 2) if total_imp else 0.0,
        "spend":       round(total_sp, 2),
        "leads":       total_lds_int,
        "cpl":         round(total_sp / total_lds_int, 2) if total_lds_int else 0.0,
    }

    logger.info(
        f"Google Ads: fetched {year}-{month:02d} → "
        f"{total_lds_int} leads, ${total_sp:.0f} spend across {len(market_totals)} markets"
    )
    return result
