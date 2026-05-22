"""PACER Case Locator scraper using Playwright — Phase 4.

Playwright runs headless Chromium so all PrimeFaces JavaScript executes
normally. Login, party search, and result refinement work exactly as they
would in a real browser — no more fighting with JSF AJAX and hidden fields.

Flow per attorney × district × chapter:
  1. Login once per collection run (session persists in browser context)
  2. Navigate to PCL party search
  3. Fill name + select "Attorney" from the role dropdown
  4. Submit → results page
  5. Use frmRefineSearch to apply court / date / chapter filters
  6. Parse the result count
  7. Store as FilingSnapshot
"""
import logging
import re
import time
from datetime import date, timedelta, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.base import new_uuid
from app.models.competitor import Competitor, CompetitorLocation
from app.models.filings import FilingSnapshot

logger = logging.getLogger(__name__)

PACER_LOGIN_URL = "https://pacer.login.uscourts.gov/csologin/login.jsf"
PCL_SEARCH_URL  = "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"

DISTRICT_TO_COURT = {"MDNC": "ncmb", "WDNC": "ncwb"}

MARKET_TO_DISTRICT = {
    "greensboro":    "MDNC",
    "winston_salem": "MDNC",
    "high_point":    "MDNC",
    "salisbury":     "MDNC",
    "charlotte":     "WDNC",
    "asheville":     "WDNC",
}

NAV_TIMEOUT    = 45_000   # ms — per navigation
ACTION_TIMEOUT = 10_000   # ms — per element action
SEARCH_DELAY   = 3.0      # seconds between searches (polite + avoids rate limits)

_SUFFIX_RE = re.compile(
    r",?\s*(Jr\.?|Sr\.?|II|III|IV|V|Esq\.?|P\.A\.?)$", re.IGNORECASE
)


# ── Public entry point ────────────────────────────────────────────────────────

def collect_filing_snapshots(db: Session) -> int:
    """
    Collect last month's PACER filing counts for all tracked attorneys.
    Called on the 1st of each month (gated in weekly.py).
    Returns number of FilingSnapshot rows saved/updated.
    """
    if not settings.pacer_username or not settings.pacer_password:
        logger.warning("PACER credentials not set — skipping filing collection")
        return 0

    today = date.today()
    period_end   = date(today.year, today.month, 1) - timedelta(days=1)
    period_start = date(period_end.year, period_end.month, 1)
    logger.info(f"PACER collection period: {period_start} → {period_end}")

    from playwright.sync_api import sync_playwright

    records = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx  = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(NAV_TIMEOUT)

        try:
            if not _login(page):
                return 0

            for comp in db.query(Competitor).filter(Competitor.active == True).all():
                for attorney in comp.attorneys:
                    name = _parse_name(attorney.attorney_name)
                    if not name:
                        continue
                    for district in _districts_for_competitor(comp):
                        court_code = DISTRICT_TO_COURT[district]
                        for chapter in (7, 13):
                            count = _search(
                                page,
                                last_name=name["last"],
                                first_name=name["first"],
                                court_code=court_code,
                                chapter=chapter,
                                period_start=period_start,
                                period_end=period_end,
                            )
                            if count is not None:
                                _upsert_snapshot(
                                    db=db,
                                    competitor_id=comp.id,
                                    attorney_id=attorney.id,
                                    district=district,
                                    chapter=chapter,
                                    period_start=period_start,
                                    period_end=period_end,
                                    case_count=count,
                                )
                                records += 1
                                logger.info(
                                    f"  {attorney.attorney_name} | "
                                    f"{district} Ch.{chapter} | "
                                    f"{period_start.strftime('%b %Y')}: {count}"
                                )
                            time.sleep(SEARCH_DELAY)

        except Exception as e:
            logger.error(f"PACER collection error: {e}", exc_info=True)
        finally:
            browser.close()

    db.commit()
    logger.info(f"PACER: saved {records} filing snapshots")
    return records


# ── Login ─────────────────────────────────────────────────────────────────────

def _login(page) -> bool:
    """Navigate to PACER login, fill credentials, and verify success."""
    try:
        page.goto(PACER_LOGIN_URL, wait_until="networkidle")

        page.fill('[name="loginForm:loginName"]', settings.pacer_username)
        page.fill('[name="loginForm:password"]',  settings.pacer_password)
        if settings.pacer_client_code:
            page.fill('[name="loginForm:clientCode"]', settings.pacer_client_code)

        # Click the login button — PrimeFaces renders it as a <button>
        page.click('[id$="fbtnLogin"]')
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)

        title = page.title()
        if "Login" in title and "Welcome" not in title:
            logger.error(f"PACER login failed — page title: {title!r}")
            return False

        logger.info(f"PACER login OK — {title.strip()}")
        return True

    except Exception as e:
        logger.error(f"PACER login error: {e}")
        return False


# ── Search ────────────────────────────────────────────────────────────────────

def _search(
    page,
    last_name: str,
    first_name: str,
    court_code: str,
    chapter: int,
    period_start: date,
    period_end: date,
) -> Optional[int]:
    """
    Run one PCL party search, apply refinement filters, and return case count.
    Returns None on error.
    """
    try:
        page.goto(PCL_SEARCH_URL, wait_until="networkidle")

        # Name fields
        page.fill('[name="frmSearch:txtPartyNameLast"]',  last_name)
        page.fill('[name="frmSearch:txtPartyNameFirst"]', first_name)

        # Party role — PrimeFaces SelectOneMenu has an underlying <select>
        _select_option(page, '[name="frmSearch:scmPartyRole"]', label="Attorney")

        # Submit
        page.click('[id$="btnSearch"]')
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)

        if "Results" not in page.title():
            logger.warning(f"No results page for {last_name}/{first_name} — {page.title()!r}")
            return 0

        # Apply filters via frmRefineSearch (now JavaScript-rendered)
        _apply_refinements(page, court_code, chapter, period_start, period_end)

        return _extract_count(page)

    except Exception as e:
        logger.error(
            f"PACER search error ({last_name}, {court_code}, Ch.{chapter}): {e}"
        )
        return None


def _select_option(page, selector: str, label: str) -> None:
    """
    Select an option by visible label from a PrimeFaces SelectOneMenu.
    Falls back to clicking if select_option doesn't work.
    """
    try:
        page.select_option(selector, label=label, timeout=ACTION_TIMEOUT)
    except Exception:
        try:
            # Click the dropdown trigger, then the option
            page.click(selector)
            page.click(f'li[data-label="{label}"]', timeout=ACTION_TIMEOUT)
        except Exception as e2:
            logger.debug(f"Could not select '{label}' on {selector}: {e2}")


def _apply_refinements(
    page,
    court_code: str,
    chapter: int,
    period_start: date,
    period_end: date,
) -> None:
    """
    Apply court / date / chapter filters on the results page via frmRefineSearch.
    Fails silently — if filters aren't available the raw count is used.
    """
    date_from = period_start.strftime("%m/%d/%Y")
    date_to   = period_end.strftime("%m/%d/%Y")

    filters = [
        # (CSS selector fragment, fill_value or None, select_label or None)
        ("court",   court_code,  None),
        ("From",    date_from,   None),
        ("To",      date_to,     None),
        ("chapter", None,        str(chapter)),
    ]

    refine_submitted = False
    for frag, fill_val, sel_label in filters:
        try:
            locator = page.locator(
                f'[id*="frmRefineSearch"][id*="{frag}"], '
                f'[name*="frmRefineSearch"][name*="{frag}"]'
            ).first
            if not locator.is_visible(timeout=2_000):
                continue
            if fill_val:
                locator.fill(fill_val)
            elif sel_label:
                try:
                    locator.select_option(value=sel_label, timeout=ACTION_TIMEOUT)
                except Exception:
                    locator.fill(sel_label)
            refine_submitted = True
        except Exception:
            pass

    if refine_submitted:
        try:
            btn = page.locator(
                'form#frmRefineSearch button[type="submit"], '
                '[id*="frmRefineSearch"][id*="btn"]'
            ).first
            if btn.is_visible(timeout=3_000):
                btn.click()
                page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        except Exception as e:
            logger.debug(f"Refine submit failed: {e}")


def _extract_count(page) -> Optional[int]:
    """
    Extract the total result count from a PCL results page.
    Returns None when the 108,000 batch-limit is hit (meaning filters
    weren't applied — the count is meaningless for our purposes).
    Returns 0 when the search genuinely found no results.
    """
    try:
        text = page.inner_text("body")

        # Detect the batch-limit message — filters not applied, count invalid
        if "108,000" in text and "batch" in text.lower():
            logger.warning("PCL batch limit hit — refinement filters not applied; skipping count")
            return None

        # "Showing 1 to 25 of 47" pagination
        m = re.search(r"\d[\d,]*\s+to\s+\d[\d,]*\s+of\s+(\d[\d,]*)", text)
        if m:
            return int(m.group(1).replace(",", ""))

        # "47 results" / "47 cases found"
        m = re.search(r"(\d[\d,]*)\s+(?:result|case)", text, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))

        # Explicit "no results" message
        if re.search(r"no (?:result|case|match)", text, re.IGNORECASE):
            return 0

        # Count table rows as last resort
        rows = page.locator(
            "table.pcl-results-table tbody tr, table[id*='result'] tbody tr"
        ).count()
        return rows if rows > 0 else None

    except Exception as e:
        logger.error(f"Count extraction failed: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_name(full_name: str) -> Optional[dict]:
    name = _SUFFIX_RE.sub("", full_name).strip()
    parts = name.split()
    if len(parts) < 2 or parts[0].lower() in ("unknown", "tbd", "n/a"):
        return None
    return {"first": parts[0], "last": parts[-1]}


def _districts_for_competitor(comp: Competitor) -> set:
    districts = set()
    for loc in comp.locations:
        d = MARKET_TO_DISTRICT.get(loc.market)
        if d:
            districts.add(d)
    return districts


def _upsert_snapshot(
    db: Session,
    competitor_id: str,
    attorney_id: str,
    district: str,
    chapter: int,
    period_start: date,
    period_end: date,
    case_count: int,
) -> None:
    existing = (
        db.query(FilingSnapshot)
        .filter(
            FilingSnapshot.competitor_id == competitor_id,
            FilingSnapshot.attorney_id   == attorney_id,
            FilingSnapshot.district      == district,
            FilingSnapshot.chapter       == chapter,
            FilingSnapshot.period_start  == period_start,
        )
        .first()
    )
    if existing:
        existing.case_count = case_count
        existing.snapped_at = datetime.now(timezone.utc)
    else:
        db.add(FilingSnapshot(
            id=new_uuid(),
            competitor_id=competitor_id,
            attorney_id=attorney_id,
            district=district,
            chapter=chapter,
            period_start=period_start,
            period_end=period_end,
            case_count=case_count,
        ))
