"""EDNC top-filer discovery — Phase 4.5.

Uses NCEB's CaseFiled-Rpt.pl to pull all bankruptcy filings for a given
month and rank attorneys by case count. Results are stored in discovery_cache
so they appear on the Filings page without manual copy-paste.
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

COURT_BASE  = "https://ecf.nceb.uscourts.gov"
LOGIN_URL   = "https://pacer.login.uscourts.gov/csologin/login.jsf"
REPORT_URL  = f"{COURT_BASE}/cgi-bin/CaseFiled-Rpt.pl"
CACHE_KEY   = "ednc_top_filers"


def run_ednc_discovery(db: Session, year: int, month: int) -> dict:
    """
    Pull NCEB Filed Cases report for the given month, parse attorney names,
    store the ranked list in discovery_cache, and return it.
    """
    if not settings.pacer_username or not settings.pacer_password:
        return {"error": "PACER credentials not configured"}

    from playwright.sync_api import sync_playwright

    period_start = date(year, month, 1)
    period_end   = date(year, month, calendar.monthrange(year, month)[1])
    logger.info(f"EDNC discovery: {period_start} → {period_end}")

    result = {
        "period":       f"{period_start.strftime('%B %Y')}",
        "top_filers":   [],
        "total_found":  0,
        "form_fields":  [],
        "result_snippet": "",
        "error":        None,
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            page.set_default_timeout(45_000)

            # Central PACER login
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.fill('[name="loginForm:loginName"]', settings.pacer_username)
            page.fill('[name="loginForm:password"]',  settings.pacer_password)
            page.click('[id$="fbtnLogin"]')
            page.wait_for_timeout(4_000)

            # NCEB court handoff
            page.goto(f"{COURT_BASE}/cgi-bin/login.pl", wait_until="domcontentloaded")
            court_title = page.title()
            if "Login" in court_title:
                result["error"] = f"NCEB auth failed: {court_title!r}"
                browser.close()
                return _store_and_return(db, result)

            # Navigate to CaseFiled-Rpt.pl
            page.goto(REPORT_URL, wait_until="domcontentloaded")
            result["report_title"] = page.title()

            # Capture form fields for diagnosis
            result["form_fields"] = page.eval_on_selector_all(
                "input, select",
                "els => els.map(e => ({name: e.name, id: e.id, type: e.type, value: e.value}))"
            )[:20]

            # Capture form fields to identify correct date field names
            result["form_fields"] = page.eval_on_selector_all(
                "input, select",
                "els => els.map(e => ({name: e.name, id: e.id, type: e.type, value: e.value}))"
            )[:25]
            logger.info(f"EDNC report form fields: {result['form_fields']}")

            # Fill date fields using every naming convention seen in CM/ECF
            date_from_str = period_start.strftime("%m/%d/%Y")
            date_to_str   = period_end.strftime("%m/%d/%Y")
            filled_from = False
            for fname in ["filed_start_dt", "date_from", "Sdate", "start_date",
                          "filed_from", "DateFiled_from", "start_dt"]:
                try:
                    page.fill(f'[name="{fname}"]', date_from_str, timeout=1_000)
                    filled_from = True
                    logger.info(f"Filled date_from via [{fname}]")
                    break
                except Exception:
                    pass
            filled_to = False
            for fname in ["filed_end_dt", "date_to", "Edate", "end_date",
                          "filed_to", "DateFiled_to", "end_dt"]:
                try:
                    page.fill(f'[name="{fname}"]', date_to_str, timeout=1_000)
                    filled_to = True
                    logger.info(f"Filled date_to via [{fname}]")
                    break
                except Exception:
                    pass
            result["date_filter_applied"] = filled_from and filled_to

            # Submit — try all common submit patterns
            for submit_sel in ['input[type="submit"]', '[name="button1"]',
                               'button[type="submit"]', 'input[value="Run Report"]',
                               'input[value="Submit"]']:
                try:
                    page.click(submit_sel, timeout=3_000)
                    page.wait_for_load_state("domcontentloaded")
                    break
                except Exception:
                    pass

            body = page.inner_text("body")
            result["result_snippet"] = body[:3000]

            # NCEB judge last names — stripped from end of attorney names
            # (judge name appears in the same text node when table cells merge)
            NCEB_JUDGES = {"Warren", "McAfee", "Callaway", "Flanagan", "Travis"}

            # Line-by-line extraction
            names = []
            for line in body.replace('\r', '\n').split('\n'):
                stripped = line.strip()
                for prefix in ("Attorney for Debtor:", "Attorney for Joint Debtor:"):
                    if stripped.startswith(prefix):
                        # Take everything after colon, stop at first tab
                        raw = stripped[len(prefix):].split('\t')[0].strip().rstrip(",.")
                        if not raw or raw.lower() in ("pro se", "unknown"):
                            break
                        # Strip trailing judge name if present
                        parts = raw.split()
                        if parts and parts[-1] in NCEB_JUDGES:
                            parts = parts[:-1]
                        name = " ".join(parts).strip().rstrip(",.")
                        if name:
                            names.append(name)
                        break
            counter = Counter(names)
            logger.info(f"EDNC: {len(names)} attorney-for-debtor lines → {len(counter)} unique attorneys")
            result["total_found"] = len(names)
            result["top_filers"] = [
                {"attorney": name, "cases": count}
                for name, count in counter.most_common(40)
            ]

            browser.close()

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"EDNC discovery error: {e}", exc_info=True)

    return _store_and_return(db, result)


def get_cached_results(db: Session) -> dict | None:
    """Return previously stored discovery results, or None."""
    from app.models.discovery import DiscoveryCache
    row = db.query(DiscoveryCache).filter(DiscoveryCache.key == CACHE_KEY).first()
    return row.value if row else None


def _store_and_return(db: Session, result: dict) -> dict:
    from app.models.discovery import DiscoveryCache
    row = db.query(DiscoveryCache).filter(DiscoveryCache.key == CACHE_KEY).first()
    if row:
        row.value      = result
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(DiscoveryCache(
            id=new_uuid(),
            key=CACHE_KEY,
            value=result,
            updated_at=datetime.now(timezone.utc),
        ))
    db.commit()
    logger.info(f"EDNC discovery stored: {len(result.get('top_filers', []))} filers found")
    return result
