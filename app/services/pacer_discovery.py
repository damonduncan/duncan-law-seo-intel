"""PACER top-filer discovery — uses CaseFiled-Rpt.pl for accurate new-filing counts.

Two different attorney-name formats exist across CM/ECF courts:

  NCEB (EDNC) — prefix format:
      Attorney for Debtor: Travis Sasser

  NCMB/NCWB (MDNC/WDNC) — backward format:
      Damon Duncan
      Duncan Law
      1000 N. Main St.
      Greensboro, NC 27401
      336-555-0100
      Attorney for Debtor        ← name is several lines above this label
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
        "judges":     {"Warren", "McAfee", "Callaway", "Flanagan", "Travis"},
        "parser":     "prefix",    # name follows colon on same line
    },
    "MDNC": {
        "court_base": "https://ecf.ncmb.uscourts.gov",
        "judges":     set(),       # add if judge names bleed into attorney cells
        "parser":     "backward",  # name precedes role label on separate line
    },
    "WDNC": {
        "court_base": "https://ecf.ncwb.uscourts.gov",
        "judges":     set(),
        "parser":     "backward",
    },
}

# ── Parsing helpers ────────────────────────────────────────────────────────────

_FIRM_SUFFIX_RE = re.compile(
    r'\b(PLLC|LLC|LLP|P\.A\.|P\.C\.|Inc\.|Corp\.|Associates|Group|Partners|Foundation)\b',
    re.IGNORECASE,
)
_FIRM_ENDING_RE = re.compile(
    r'\b(Law|Firm|Office|Center|Legal|Counsel)\s*$',
    re.IGNORECASE,
)
# Firm names often start with these patterns — reject before spending time on other checks
_FIRM_PREFIX_RE = re.compile(
    r'^(Law Office|Law Offices|Office of|The Law|Chapter \d|Case No)',
    re.IGNORECASE,
)
_DIGITS_RE   = re.compile(r'\d{3,}')            # phone numbers, zip codes, street numbers
_STATE_ZIP   = re.compile(r',\s*[A-Z]{2}\s+\d') # "Greensboro, NC 2"
_SKIP_PREFIX = re.compile(
    r'^(Role:|Filed:|Entered:|Office:|Chapter:|Lead BK:|PACER|U\.S\.|Case No\.|Debtor:|Case:)',
    re.IGNORECASE,
)
_SKIP_EXACT  = {"pro se", "all", "open", "closed", ""}

# Matches "Attorney for Debtor", "Attorneys for Debtor", "Attorney for Debtor(s)",
# "Attorney for Joint Debtor", "Attorneys for Joint Debtors", etc.
_ATTY_ROLE_RE = re.compile(
    r'^Attorneys?\s+for\s+(?:Joint\s+)?Debtors?',
    re.IGNORECASE,
)


def _is_person_name(s: str) -> bool:
    """Return True if s looks like an individual attorney's name."""
    if not s or s.lower() in _SKIP_EXACT:
        return False
    words = s.split()
    if len(words) < 2:
        return False
    if len(words) > 5:              # firm names have many words; person names rarely exceed 5
        return False
    if not words[0][0].isupper():
        return False
    if _DIGITS_RE.search(s):        # any 3+ digit run → address/phone/zip
        return False
    if _FIRM_PREFIX_RE.match(s):    # starts like a firm name ("Law Office of…")
        return False
    if _FIRM_SUFFIX_RE.search(s):   # firm indicators (PLLC, LLC, etc.)
        return False
    if _FIRM_ENDING_RE.search(s):   # name ends in Law, Firm, Office, etc.
        return False
    if _STATE_ZIP.search(s):        # "City, NC 27401"
        return False
    if _SKIP_PREFIX.match(s):       # "Role:", "Filed:", etc.
        return False
    if '&' in s:                    # partnership / firm name
        return False
    return True


def _parse_prefix(body: str, judges: set) -> list:
    """
    NCEB format: extract name from 'Attorney for Debtor: <Name>' lines.
    """
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
    return names


def _parse_backward(body: str, judges: set) -> list:
    """
    NCMB/NCWB format: attorney name appears on the line(s) above the role label.
    Scan backwards from each 'Attorney for Debtor' label to find the name.
    Handles role label variations: "Attorneys for Debtor(s)", "Attorney for Joint Debtors", etc.
    """
    lines = body.replace("\r", "\n").split("\n")
    names = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip prefix-format lines like "Attorney for Debtor: Name" — name already on same line
        if ':' in stripped:
            continue
        if not _ATTY_ROLE_RE.match(stripped):
            continue
        # Scan back up to 15 lines to find the attorney name above this label
        for j in range(i - 1, max(-1, i - 15), -1):
            candidate = lines[j].strip()
            if not candidate:
                continue
            if _is_person_name(candidate):
                parts = candidate.split()
                if parts and parts[-1] in judges:
                    parts = parts[:-1]
                if parts:
                    names.append(" ".join(parts))
                break
    return names


def _parse_attorneys(body: str, district: str) -> list:
    config = DISTRICT_CONFIG.get(district.upper(), {})
    judges = config.get("judges", set())
    if config.get("parser") == "backward":
        return _parse_backward(body, judges)
    return _parse_prefix(body, judges)


# ── Cache key ──────────────────────────────────────────────────────────────────

def _cache_key(district: str) -> str:
    # EDNC keeps its legacy key so existing cached results are not lost
    if district.upper() == "EDNC":
        return "ednc_top_filers"
    return f"{district.lower()}_top_filers"


# ── Main discovery function ────────────────────────────────────────────────────

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
            result["form_fields"] = page.eval_on_selector_all(
                "input, select",
                "els => els.map(e => ({name: e.name, id: e.id, type: e.type, value: e.value}))"
            )[:25]
            logger.info(f"{district} form fields: {result['form_fields']}")

            # Fill date range
            # NCMB/NCWB use StartDate/EndDate; NCEB uses filed_start_dt
            date_from_str = period_start.strftime("%m/%d/%Y")
            date_to_str   = period_end.strftime("%m/%d/%Y")
            for fname in ["StartDate", "filed_start_dt", "date_from", "Sdate",
                          "start_date", "filed_from", "DateFiled_from"]:
                try:
                    page.fill(f'[name="{fname}"]', date_from_str, timeout=1_000)
                    logger.info(f"Filled date_from via [{fname}]")
                    break
                except Exception:
                    pass
            for fname in ["EndDate", "filed_end_dt", "date_to", "Edate",
                          "end_date", "filed_to", "DateFiled_to"]:
                try:
                    page.fill(f'[name="{fname}"]', date_to_str, timeout=1_000)
                    logger.info(f"Filled date_to via [{fname}]")
                    break
                except Exception:
                    pass

            # Ensure party/attorney info checkbox is checked — without it, attorney
            # names don't appear in the CaseFiled-Rpt.pl output.
            # Different CM/ECF courts use different ids/names for this checkbox.
            _party_selectors = [
                '[id="party_information"]',
                '[name="party_information"]',
                '[id="party_info"]',
                '[name="party_info"]',
                '[id="include_party"]',
                '[name="include_party"]',
                'input[type="checkbox"][id*="party"]',
                'input[type="checkbox"][name*="party"]',
                'input[type="checkbox"][id*="attorney"]',
                'input[type="checkbox"][name*="attorney"]',
            ]
            _party_checked = False
            for _sel in _party_selectors:
                try:
                    if page.locator(_sel).count() > 0:
                        if not page.is_checked(_sel):
                            page.check(_sel, timeout=1_500)
                        _party_checked = True
                        logger.info(f"{district}: party checkbox checked via {_sel!r}")
                        break
                except Exception:
                    pass
            if not _party_checked:
                logger.warning(f"{district}: could not find party/attorney info checkbox — attorney names may be missing from output")
            result["party_checkbox_found"] = _party_checked

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

            # Count trigger lines found (diagnostic) — helps diagnose parse failures
            _trigger_lines = [
                l.strip() for l in body.replace("\r", "\n").split("\n")
                if _ATTY_ROLE_RE.match(l.strip()) and ':' not in l.strip()
            ]
            result["trigger_lines_found"] = len(_trigger_lines)
            result["trigger_sample"] = _trigger_lines[:3]
            logger.info(f"{district}: {len(_trigger_lines)} 'Attorney for Debtor' trigger lines found in body")

            names = _parse_attorneys(body, district)
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


# Backward-compatible alias
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
