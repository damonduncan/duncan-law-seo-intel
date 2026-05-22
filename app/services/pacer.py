"""PACER Case Locator scraper — Phase 4.

For each tracked attorney, queries the PACER Case Locator (PCL) to count
Chapter 7 and Chapter 13 bankruptcy filings in MDNC (ncmb) and WDNC (ncwb)
for a given monthly period.

PACER charges $0.10/page of results. Estimated cost: ~$1–5/month for the
full 25-attorney roster across both districts and both chapters.

Authentication flow:
  1. GET pacer.login.uscourts.gov/csologin/login.jsf → extract JSF ViewState
  2. POST login form with credentials → authenticated session cookies
  3. GET/POST pcl.uscourts.gov attorney search → parse result count
"""
import logging
import re
import time
from datetime import date, timedelta, datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config import settings
from app.models.base import new_uuid
from app.models.competitor import Competitor, CompetitorLocation
from app.models.filings import FilingSnapshot

logger = logging.getLogger(__name__)

PACER_LOGIN_URL = "https://pacer.login.uscourts.gov/csologin/login.jsf"
PCL_SEARCH_PAGE = "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"

# PACER court codes for NC bankruptcy courts
DISTRICT_TO_COURT = {
    "MDNC": "ncmb",
    "WDNC": "ncwb",
}

MARKET_TO_DISTRICT = {
    "greensboro":    "MDNC",
    "winston_salem": "MDNC",
    "high_point":    "MDNC",
    "salisbury":     "MDNC",
    "charlotte":     "WDNC",
    "asheville":     "WDNC",
}

# Suffixes to strip before parsing first/last name
_SUFFIX_RE = re.compile(
    r",?\s*(Jr\.?|Sr\.?|II|III|IV|V|Esq\.?|P\.A\.?)$", re.IGNORECASE
)
REQUEST_DELAY = 2.0


# ── Public entry point ────────────────────────────────────────────────────────

def collect_filing_snapshots(db: Session) -> int:
    """
    Collect last month's PACER filing counts for all tracked attorneys.
    Call on the 1st of each month (gated in weekly.py).
    Returns number of FilingSnapshot rows saved/updated.
    """
    if not settings.pacer_username or not settings.pacer_password:
        logger.warning("PACER credentials not set — skipping filing collection")
        return 0

    today = date.today()
    period_end = date(today.year, today.month, 1) - timedelta(days=1)
    period_start = date(period_end.year, period_end.month, 1)
    logger.info(f"PACER collection period: {period_start} → {period_end}")

    session = _pacer_login()
    if not session:
        return 0

    records = 0
    competitors = db.query(Competitor).filter(Competitor.active == True).all()

    for comp in competitors:
        comp_districts = _districts_for_competitor(comp)
        for attorney in comp.attorneys:
            name = _parse_name(attorney.attorney_name)
            if not name:
                continue
            for district in comp_districts:
                court_code = DISTRICT_TO_COURT[district]
                for chapter in (7, 13):
                    count = _search_pcl(
                        session=session,
                        last_name=name["last"],
                        first_name=name["first"],
                        court_code=court_code,
                        chapter=chapter,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    if count is None:
                        logger.warning(
                            f"PCL search failed: {attorney.attorney_name} "
                            f"Ch.{chapter} {district}"
                        )
                        continue

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
                        f"  {attorney.attorney_name} | {district} Ch.{chapter} | "
                        f"{period_start.strftime('%b %Y')}: {count} cases"
                    )
                    time.sleep(REQUEST_DELAY)

    db.commit()
    logger.info(f"PACER: saved {records} filing snapshots")
    return records


# ── PACER authentication ──────────────────────────────────────────────────────

def _pacer_login() -> Optional[requests.Session]:
    """Log in to PACER and return an authenticated requests.Session, or None."""
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    try:
        resp = session.get(PACER_LOGIN_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # The page has multiple forms; the login form is the one with a password input
        login_form = _find_form_with_password(soup)
        if not login_form:
            logger.error("PACER: could not locate login form on page")
            return None

        form_data = _all_inputs(login_form)

        # Auto-detect the actual field names for username and password
        user_field = _detect_field(login_form, ("text",), ("name", "login", "user"))
        pass_field = _detect_field(login_form, ("password",), ())
        code_field = _detect_field(login_form, ("text",), ("client", "code"))

        if user_field:
            form_data[user_field] = settings.pacer_username
        if pass_field:
            form_data[pass_field] = settings.pacer_password
        if code_field:
            form_data[code_field] = settings.pacer_client_code or ""

        # Include any submit button value
        for btn in login_form.find_all(["input", "button"]):
            if btn.get("type", "").lower() == "submit" and btn.get("name"):
                form_data[btn["name"]] = btn.get("value", "Login")

        logger.debug(
            f"PACER login: form fields detected: user={user_field}, pass={pass_field}, "
            f"code={code_field}, all_names={list(form_data.keys())}"
        )

        resp = session.post(PACER_LOGIN_URL, data=form_data, timeout=30,
                            allow_redirects=True)

        # Success check: login page title disappears after successful auth
        if "PACER: Login" in resp.text or "pacer: login" in resp.text.lower():
            logger.error(
                "PACER login failed — response still shows login page. "
                "Check PACER_USERNAME / PACER_PASSWORD in Railway env vars."
            )
            return None

        logger.info("PACER login successful")
        return session

    except Exception as e:
        logger.error(f"PACER login error: {e}")
        return None


# ── PCL search ────────────────────────────────────────────────────────────────

def _search_pcl(
    session: requests.Session,
    last_name: str,
    first_name: str,
    court_code: str,
    chapter: int,
    period_start: date,
    period_end: date,
) -> Optional[int]:
    """
    Search the PACER Case Locator for matching cases.
    Returns the total case count, or None on error.
    """
    try:
        # GET search page; use the content form (not the nav form)
        resp = session.get(PCL_SEARCH_PAGE, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        search_form = _find_content_form(soup)
        form_data = _all_inputs(search_form) if search_form else {}

        date_range = (
            f"{period_start.strftime('%m/%d/%Y')} "
            f"to {period_end.strftime('%m/%d/%Y')}"
        )

        # Overlay search parameters using the expected JSF field names.
        # These match the PACER PCL's findPartyForm component structure.
        form_data.update({
            "findPartyForm:partyType": "at",   # attorney
            "findPartyForm:lastName":  last_name,
            "findPartyForm:firstName": first_name,
            "findPartyForm:courtType": "bk",
            "findPartyForm:courtId":   court_code,
            "findPartyForm:dateFiled": date_range,
            "findPartyForm:chapter":   str(chapter),
            "findPartyForm:btnSearch": "Search",
        })

        resp = session.post(PCL_SEARCH_PAGE, data=form_data, timeout=30)
        resp.raise_for_status()

        count = _parse_result_count(resp.text)
        logger.debug(
            f"PCL {last_name}/{court_code}/Ch{chapter}: {count} cases"
        )
        return count

    except Exception as e:
        logger.error(
            f"PCL search error ({last_name}, {court_code}, Ch.{chapter}): {e}"
        )
        return None


def _parse_result_count(html: str) -> int:
    """
    Extract total result count from PCL results page.
    Looks for patterns like:
      "Showing 1 to 10 of 47 results"
      "47 cases found"
      A results table row count
    Returns 0 if no results are found or pattern is not recognized.
    """
    soup = BeautifulSoup(html, "lxml")

    # Pattern 1: "X to Y of Z" paging text
    paging = soup.find(string=re.compile(r"\d+\s+to\s+\d+\s+of\s+\d+", re.IGNORECASE))
    if paging:
        m = re.search(r"of\s+(\d[\d,]*)", paging, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))

    # Pattern 2: "N results" or "N cases"
    count_text = soup.find(string=re.compile(r"\d+\s+(result|case)", re.IGNORECASE))
    if count_text:
        m = re.search(r"(\d[\d,]*)", count_text)
        if m:
            return int(m.group(1).replace(",", ""))

    # Pattern 3: "No cases found" / "no records"
    page_lower = html.lower()
    if any(p in page_lower for p in ("no cases found", "no records", "no results", "0 case")):
        return 0

    # Pattern 4: Count result rows in the main data table (fallback)
    table = soup.find("table", {"class": re.compile(r"result", re.IGNORECASE)})
    if not table:
        table = soup.find("table", id=re.compile(r"result", re.IGNORECASE))
    if table:
        rows = table.find_all("tr")[1:]  # skip header
        if rows:
            return len(rows)

    logger.warning("PCL: could not parse result count from response")
    return 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_inputs(form) -> dict:
    """Return name→value for every input/select in a form element."""
    data = {}
    if not form:
        return data
    for inp in form.find_all(["input", "select"]):
        name = inp.get("name")
        if name:
            data[name] = inp.get("value", "")
    return data


def _find_form_with_password(soup: BeautifulSoup):
    """Return the first form that contains a password-type input."""
    for form in soup.find_all("form"):
        if form.find("input", {"type": "password"}):
            return form
    return None


def _find_content_form(soup: BeautifulSoup):
    """
    Return the main content form — i.e., not the navbar form.
    PACER pages have a cbMenuForm nav form first; skip it.
    """
    forms = soup.find_all("form")
    # Skip any form whose id/name contains "menu" or "nav"
    for form in forms:
        form_id = (form.get("id") or form.get("name") or "").lower()
        if "menu" not in form_id and "nav" not in form_id:
            return form
    return forms[-1] if forms else None


def _detect_field(form, input_types: tuple, name_hints: tuple) -> Optional[str]:
    """
    Find an input field in a form by type and/or name keyword hints.
    Returns the field's name attribute, or None.
    """
    for inp in form.find_all("input"):
        inp_type = (inp.get("type") or "text").lower()
        inp_name = (inp.get("name") or "").lower()
        if input_types and inp_type not in input_types:
            continue
        if not name_hints:
            return inp.get("name")
        if any(h in inp_name for h in name_hints):
            return inp.get("name")
    return None


def _parse_name(full_name: str) -> Optional[dict]:
    """
    Parse 'Matthew T. McKee' → {'first': 'Matthew', 'last': 'McKee'}.
    Returns None for placeholder names like 'Unknown'.
    """
    name = _SUFFIX_RE.sub("", full_name).strip()
    parts = name.split()
    if len(parts) < 2 or parts[0].lower() in ("unknown", "tbd", "n/a"):
        return None
    return {"first": parts[0], "last": parts[-1]}


def _districts_for_competitor(comp: Competitor) -> set:
    """Return the set of PACER districts this competitor operates in."""
    districts = set()
    for loc in comp.locations:
        district = MARKET_TO_DISTRICT.get(loc.market)
        if district:
            districts.add(district)
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
            FilingSnapshot.attorney_id == attorney_id,
            FilingSnapshot.district == district,
            FilingSnapshot.chapter == chapter,
            FilingSnapshot.period_start == period_start,
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
