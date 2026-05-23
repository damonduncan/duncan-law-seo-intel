"""PACER top-filer discovery — uses CaseFiled-Rpt.pl for accurate new-filing counts.

CaseFiled-Rpt.pl is a standard CM/ECF report available on all federal bankruptcy
courts. It lists every case opened in a date range with the attorney of record,
giving an exact count of new filings per attorney per month — which is what
iquery.pl does NOT reliably provide (iquery counts attorney activity across all
their cases, not just new filings).

Works for all three NC bankruptcy districts: MDNC, WDNC, EDNC.
"""
import logging
import re
from collections import Counter
from datetime import datetime, timezone, date
import calendar

from sqlalchemy.orm import Session

from app.config import settings
from app.models.base import new_uuid

logger = logging.getLogger(__name__)

LOGIN_URL = "https://pacer.login.uscourts.gov/csologin/login.jsf"

DISTRICT_CONFIG = {
    "EDNC": {
        "court_base": "https://ecf.nceb.uscourts.gov",
        # Judge last names that bleed into attorney name cells in NCEB's rendering
        "judges": {"Warren", "McAfee", "Callaway", "Flanagan", "Travis"},
    },
    "MDNC": {
        "court_base": "https://ecf.ncmb.uscourts.gov",
        "judges": set(),  # populate after first run if judge names appear in output
    },
    "WDNC": {
        "court_base": "https://ecf.ncwb.uscourts.gov",
        "judges": set(),  # populate after first run if judge names appear in output
    },
}


def _cache_key(district: str) -> str:
    # EDNC keeps its legacy key so existing cached results are not lost
    if district.upper() == "EDNC":
        return "ednc_top_filers"
    return f"{district.lower()}_top_filers"


def run_district_discovery(db: Session, district: str, year: int, month: int) -> dict:
    """
    Pull CaseFiled-Rpt.pl for the given district and month, parse attorney names,
    store the ranked list in discovery_cache, and return it.
    district must be one of: MDNC, WDNC, EDNC.
    """
    if not settings.pacer_username or not settings.pacer_password:
        return {"error": "PACER credentials not configured"}

    district = district.upper()
    config = DISTRICT_CONFIG.get(district)
    if not config:
        return {"error": f"Unknown district: {district}"}

    from playwright.sync_api import sync_playwright

    court_base = config["court_base"]
    judges     = config["judges"]
    report_url = f"{court_base}/cgi-bin/CaseFiled-Rpt.pl"

    period_start = date(year, month, 1)
    period_end   = date(year, month, calendar.monthrange(year, month)[1])
    logger.info(f"{district} discovery: {period_start} → {period_end}")

    result = {
        "district":       district,
        "period":         period_start.strftime("%B %Y"),
        "top_filers":     [],
        "total_found":    0,
        "form_fields":    [],
        "result_snippet": "",
        "error":          None,
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.set_default_timeout(45_000)

            # Central PACER login
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.fill('[name="loginForm:loginName"]', settings.pacer_username)
            page.fill('[name="loginForm:password"]',  settings.pacer_password)
            page.click('[id$="fbtnLogin"]')
            page.wait_for_timeout(4_000)

            # Court-specific handoff
            page.goto(f"{court_base}/cgi-bin/login.pl", wait_until="domcontentloaded")
            court_title = page.title()
            if "Login" in court_title:
                result["error"] = f"{district} auth failed: {court_title!r}"
                browser.close()
                return _store_and_return(db, result, district)

            # Navigate to CaseFiled-Rpt.pl
            page.goto(report_url, wait_until="domcontentloaded")
            result["report_title"] = page.title()
            result["form_fields"]  = page.eval_on_selector_all(
                "input, select",
                "els => els.map(e => ({name: e.name, id: e.id, type: e.type, value: e.value}))"
            )[:25]
            logger.info(f"{district} form fields: {result['form_fields']}")

            # Fill date range — try every naming convention seen across CM/ECF courts
            date_from_str = period_start.strftime("%m/%d/%Y")
            date_to_str   = period_end.strftime("%m/%d/%Y")
            for fname in ["filed_start_dt", "date_from", "Sdate", "start_date",
                          "filed_from", "DateFiled_from", "start_dt"]:
                try:
                    page.fill(f'[name="{fname}"]', date_from_str, timeout=1_000)
                    logger.info(f"Filled date_from via [{fname}]")
                    break
                except Exception:
                    pass
            for fname in ["filed_end_dt", "date_to", "Edate", "end_date",
                          "filed_to", "DateFiled_to", "end_dt"]:
                try:
                    page.fill(f'[name="{fname}"]', date_to_str, timeout=1_000)
                    logger.info(f"Filled date_to via [{fname}]")
                    break
                except Exception:
                    pass

            # Submit
            for sel in ['input[type="submit"]', '[name="button1"]',
                        'button[type="submit"]', 'input[value="Run Report"]',
                        'input[value="Submit"]']:
                try:
                    page.click(sel, timeout=3_000)
                    page.wait_for_load_state("domcontentloaded")
                    break
                except Exception:
                    pass

            body = page.inner_text("body")
            result["result_snippet"] = body[:3000]

            # Line-by-line extraction of attorney names from "Attorney for Debtor:" lines
            names = []
            for line in body.replace("\r", "\n").split("\n"):
                stripped = line.strip()
                for prefix in ("Attorney for Debtor:", "Attorney for Joint Debtor:"):
                    if stripped.startswith(prefix):
                        raw = stripped[len(prefix):].split("\t")[0].strip().rstrip(",.")
                        if not raw or raw.lower() in ("pro se", "unknown"):
                            break
                        parts = raw.split()
                        if parts and parts[-1] in judges:
                            parts = parts[:-1]
                        name = " ".join(parts).strip().rstrip(",.")
                        if name:
                            names.append(name)
                        break

            counter = Counter(names)
            logger.info(
                f"{district}: {len(names)} attorney lines → "
                f"{len(counter)} unique attorneys"
            )
            result["total_found"] = len(names)
            result["top_filers"]  = [
                {"attorney": name, "cases": count}
                for name, count in counter.most_common(40)
            ]

            browser.close()

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"{district} discovery error: {e}", exc_info=True)

    return _store_and_return(db, result, district)


# Backward-compatible alias so existing call sites don't break
def run_ednc_discovery(db: Session, year: int, month: int) -> dict:
    return run_district_discovery(db, "EDNC", year, month)


def get_cached_results(db: Session, district: str = "EDNC") -> dict | None:
    """Return previously stored discovery results for a district, or None."""
    from app.models.discovery import DiscoveryCache
    row = (
        db.query(DiscoveryCache)
        .filter(DiscoveryCache.key == _cache_key(district))
        .first()
    )
    return row.value if row else None


def _store_and_return(db: Session, result: dict, district: str) -> dict:
    from app.models.discovery import DiscoveryCache
    key = _cache_key(district)
    row = db.query(DiscoveryCache).filter(DiscoveryCache.key == key).first()
    if row:
        row.value      = result
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(DiscoveryCache(
            id=new_uuid(),
            key=key,
            value=result,
            updated_at=datetime.now(timezone.utc),
        ))
    db.commit()
    logger.info(f"{district} discovery stored: {len(result.get('top_filers', []))} filers")
    return result
