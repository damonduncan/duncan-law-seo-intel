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

DISTRICT_TO_COURT = {"MDNC": "ncmb", "WDNC": "ncwb", "EDNC": "nceb"}

MARKET_TO_DISTRICT = {
    "greensboro":    "MDNC",
    "winston_salem": "MDNC",
    "high_point":    "MDNC",
    "salisbury":     "MDNC",
    "charlotte":     "WDNC",
    "asheville":     "WDNC",
    # Eastern NC market tag — used for competitors with EDNC presence
    "ednc":          "EDNC",
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

    # Group attorneys by district up front
    by_district: dict = {"MDNC": [], "WDNC": [], "EDNC": []}
    for comp in db.query(Competitor).filter(Competitor.active == True).all():
        for attorney in comp.attorneys:
            name = _parse_name(attorney.attorney_name)
            if not name:
                continue
            for district in _districts_for_competitor(comp):
                by_district[district].append((comp, attorney, name))

    records = 0

    # ── One fresh browser per district ───────────────────────────────────────
    # Opening a new Playwright session per district keeps peak memory usage
    # low — Chromium is fully released before the next district starts.
    from playwright.sync_api import sync_playwright

    for district, attorney_list in by_district.items():
        if not attorney_list:
            continue

        court_code = DISTRICT_TO_COURT[district]
        court_base = f"https://ecf.{court_code}.uscourts.gov"
        logger.info(f"Starting {district} ({len(attorney_list)} attorneys) …")

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = browser.new_context().new_page()
                page.set_default_timeout(NAV_TIMEOUT)

                # Central login
                if not _login(page):
                    logger.error(f"PACER login failed for {district} — skipping")
                    browser.close()
                    continue

                # Court-specific handoff
                try:
                    page.goto(f"{court_base}/cgi-bin/login.pl",
                              wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                    court_title = page.title()
                    if "Login" in court_title:
                        logger.error(f"Court auth failed for {district}: {court_title!r}")
                        browser.close()
                        continue
                    logger.info(f"Court auth OK: {court_code} — {court_title!r}")
                except Exception as e:
                    logger.error(f"Court handoff error {court_code}: {e}")
                    browser.close()
                    continue

                # Run all attorney searches for this district
                for comp, attorney, name in attorney_list:
                    chapter_counts = _search(
                        page,
                        last_name=name["last"],
                        first_name=name["first"],
                        court_code=court_code,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    if chapter_counts is not None:
                        for chapter, count in chapter_counts.items():
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
                            f"  {attorney.attorney_name} | {district} | "
                            f"{period_start.strftime('%b %Y')}: "
                            f"Ch7={chapter_counts[7]} Ch13={chapter_counts[13]}"
                        )
                    time.sleep(SEARCH_DELAY)

                browser.close()

        except Exception as e:
            logger.error(f"PACER {district} collection error: {e}", exc_info=True)

        # Commit after each district so partial results are always saved
        db.commit()
        logger.info(f"{district} done — {records} total snapshots so far")

    logger.info(f"PACER collection complete: {records} snapshots saved")
    return records


# ── Login ─────────────────────────────────────────────────────────────────────

def _login(page) -> bool:
    """
    Login to PACER central. Called once per collection run.
    Court-specific auth handoffs (login.pl) are handled separately in
    the collection loop so we never hit PACER central twice in one run.
    """
    try:
        page.goto(PACER_LOGIN_URL, wait_until="domcontentloaded")

        page.fill('[name="loginForm:loginName"]', settings.pacer_username)
        page.fill('[name="loginForm:password"]',  settings.pacer_password)
        if settings.pacer_client_code:
            page.fill('[name="loginForm:clientCode"]', settings.pacer_client_code)

        page.click('[id$="fbtnLogin"]')
        page.wait_for_timeout(4_000)   # wait for PACER AJAX redirect cycle

        post_url = page.url
        logger.debug(f"Post-login url={post_url}")

        # PACER AJAX always returns to login.jsf even on success —
        # session cookies are set regardless. We treat this as success
        # and verify per-court access via login.pl handoffs.
        logger.info("PACER central login completed — will verify via court login.pl")
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
    period_start: date,
    period_end: date,
) -> Optional[dict]:
    """
    Query CM/ECF iquery.pl for an attorney's cases in a date range.
    Does ONE search and counts Chapter 7 and 13 from the results text.
    Returns {7: count, 13: count} or None on error.

    Field names confirmed from debug: last_name, first_name, filed_from,
    filed_to, person_type (select: "aty"), button1 (type=button).
    nature_suit is adversary proceeding types — not used for chapter filtering.
    """
    court_base = f"https://ecf.{court_code}.uscourts.gov"
    iquery_url = f"{court_base}/cgi-bin/iquery.pl"

    try:
        page.goto(iquery_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        title = page.title()

        if "Login" in title and "Database" not in title:
            logger.warning(f"CM/ECF session invalid for {court_code} — {title!r}")
            return None

        page.fill('[name="last_name"]',  last_name)
        page.fill('[name="first_name"]', first_name)
        page.fill('[name="filed_from"]', period_start.strftime("%m/%d/%Y"))
        page.fill('[name="filed_to"]',   period_end.strftime("%m/%d/%Y"))

        # Attorney party type
        try:
            page.select_option('[name="person_type"]', value="aty", timeout=ACTION_TIMEOUT)
        except Exception:
            try:
                page.select_option('[name="person_type"]', label="Attorney", timeout=ACTION_TIMEOUT)
            except Exception as e:
                logger.debug(f"person_type select: {e}")

        # Include both open and closed cases
        for cb in ["open_cases", "closed_cases"]:
            try:
                if not page.is_checked(f'[id="{cb}"]'):
                    page.check(f'[id="{cb}"]', timeout=1_500)
            except Exception:
                pass

        # Submit — button1 is type="button", not type="submit"
        page.click('[name="button1"]', timeout=ACTION_TIMEOUT)
        page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)

        text = page.inner_text("body")
        counts = _parse_chapter_counts(text)
        logger.debug(
            f"CM/ECF {last_name}/{court_code}: "
            f"Ch7={counts[7]} Ch13={counts[13]}"
        )
        return counts

    except Exception as e:
        logger.error(f"CM/ECF search error ({last_name}, {court_code}): {e}")
        return None


def _parse_chapter_counts(text: str) -> dict:
    """
    Count Chapter 7 and Chapter 13 cases from CM/ECF results text.
    Each result row has tab-delimited columns; chapter appears as \\t7\\t or \\t13\\t.
    Verified against Damon Duncan / MDNC / April 2026 results.
    """
    ch7  = text.count("\t7\t")
    ch13 = text.count("\t13\t")
    return {7: ch7, 13: ch13}


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
    Logs the rendered form structure on first call so we can inspect selectors.
    Fails silently — if filters aren't available the raw count is used.
    """
    # Inspect the rendered refinement form on the first call — logged to Railway
    try:
        rendered = page.evaluate("""() => {
            const form = document.getElementById('frmRefineSearch');
            if (!form) return {found: false, html: null, inputs: []};
            return {
                found: true,
                html: form.innerHTML.substring(0, 2000),
                inputs: Array.from(form.querySelectorAll('input,select,button,textarea'))
                    .map(el => ({
                        tag: el.tagName, name: el.name, id: el.id,
                        type: el.type || null,
                        visible: el.offsetParent !== null,
                        value: el.value || null
                    }))
            };
        }""")
        if rendered.get("found"):
            logger.info(f"frmRefineSearch inputs: {rendered.get('inputs')}")
            logger.info(f"frmRefineSearch html[:500]: {(rendered.get('html') or '')[:500]}")
        else:
            logger.warning("frmRefineSearch not found on results page — no filters applied")
    except Exception as e:
        logger.debug(f"Refinement form inspection failed: {e}")

    date_from = period_start.strftime("%m/%d/%Y")
    date_to   = period_end.strftime("%m/%d/%Y")

    # Try both specific and broad selectors for each filter
    filter_attempts = [
        # (selector, value, use_select)
        ('[id*="RefineSearch"][id*="court"],[name*="RefineSearch"][name*="court"]', court_code, False),
        ('[id*="RefineSearch"][id*="Court"],[name*="RefineSearch"][name*="Court"]', court_code, False),
        ('[id*="RefineSearch"][id*="From"],[name*="RefineSearch"][name*="From"]',   date_from,  False),
        ('[id*="RefineSearch"][id*="from"],[name*="RefineSearch"][name*="from"]',   date_from,  False),
        ('[id*="RefineSearch"][id*="To"],[name*="RefineSearch"][name*="To"]',       date_to,    False),
        ('[id*="RefineSearch"][id*="to"],[name*="RefineSearch"][name*="to"]',       date_to,    False),
        ('[id*="RefineSearch"][id*="chapter"],[name*="RefineSearch"][name*="chapter"]', str(chapter), True),
        ('[id*="RefineSearch"][id*="Chapter"],[name*="RefineSearch"][name*="Chapter"]', str(chapter), True),
    ]

    refine_submitted = False
    for selector, value, use_select in filter_attempts:
        try:
            loc = page.locator(selector).first
            if not loc.is_visible(timeout=1_500):
                continue
            if use_select:
                try:
                    loc.select_option(value=value, timeout=3_000)
                except Exception:
                    loc.fill(value)
            else:
                loc.fill(value)
            refine_submitted = True
            logger.debug(f"Applied filter via '{selector}' = {value}")
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
    # Skip single-letter initials (e.g. "R. Todd Mosley" → first="Todd")
    # PACER won't match "R." against a full first name like "Robert"
    first = parts[0]
    if len(first.rstrip(".")) == 1 and len(parts) > 2:
        first = parts[1]
    return {"first": first, "last": parts[-1]}


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
