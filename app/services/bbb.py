"""BBB profile scraper — Phase 3.

Scrapes letter grade, customer star rating, review count, and complaint count
from each competitor's BBB profile URL (configured in competitors.yaml).
Uses requests + BeautifulSoup; BBB renders key data server-side.
"""
import logging
import re
import time
from decimal import Decimal
from typing import Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.models.base import new_uuid
from app.models.competitor import Competitor
from app.models.reviews import ReviewSnapshot

logger = logging.getLogger(__name__)

REQUEST_DELAY = 2.0  # seconds between requests — be polite to BBB
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Letter grade pattern: A+, A, B+, B, C+, C, D+, D, E, F, NR
_GRADE_RE = re.compile(r'^(?:[A-F][+-]?|NR)$')
_STAR_RE = re.compile(r'([\d.]+)\s*(?:out of|/)\s*5', re.IGNORECASE)
_COUNT_RE = re.compile(r'(\d[\d,]*)\s+[Cc]ustomer\s+[Rr]eview')
_COMPLAINT_RE = re.compile(r'(\d[\d,]*)\s+[Cc]omplaint')


def collect_bbb_reviews(db: Session) -> int:
    """Scrape BBB profiles for all active competitors that have a bbb_url. Returns row count."""
    comps = (
        db.query(Competitor)
        .filter(
            Competitor.active == True,
            Competitor.bbb_url != None,
            Competitor.bbb_url != "",
        )
        .all()
    )

    records = 0
    for comp in comps:
        data = _scrape_bbb(comp.bbb_url)
        if data:
            snap = ReviewSnapshot(
                id=new_uuid(),
                competitor_id=comp.id,
                market=None,
                source="bbb",
                rating=Decimal(str(data["star_rating"])) if data.get("star_rating") else None,
                review_count=data.get("review_count"),
                snapshot_data=data,
            )
            db.add(snap)
            records += 1
        time.sleep(REQUEST_DELAY)

    db.commit()
    logger.info(f"BBB scraper: saved {records} snapshots")
    return records


def _scrape_bbb(url: str) -> Optional[dict]:
    """Fetch and parse a BBB profile page. Returns a dict of extracted fields."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 404:
            logger.warning(f"BBB: page not found: {url}")
            return None
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"BBB fetch error for {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    page_text = soup.get_text(" ", strip=True)
    result: dict = {"url": url}

    # ── Letter grade ──────────────────────────────────────────────────────────
    # Try CSS selectors first (BBB renders the grade in a <p> or <span> near "BBB Rating")
    grade = None
    for sel in [
        "[class*='grade'] p", "[class*='Grade'] p",
        "[class*='grade'] span", "[class*='Grade'] span",
        "[data-testid*='grade']", "[data-testid*='Grade']",
    ]:
        el = soup.select_one(sel)
        if el:
            candidate = el.get_text(strip=True)
            if _GRADE_RE.match(candidate):
                grade = candidate
                break

    # Fallback: scan every short text node for a valid grade
    if not grade:
        for node in soup.find_all(string=True):
            candidate = node.strip()
            if _GRADE_RE.match(candidate):
                # Confirm there's a nearby "rating" or "BBB" label
                parent_text = (node.parent.get_text(" ") if node.parent else "")
                if "bbb" in parent_text.lower() or "rating" in parent_text.lower():
                    grade = candidate
                    break

    if grade:
        result["letter_grade"] = grade

    # ── Customer star rating ──────────────────────────────────────────────────
    # BBB uses aria-label="X out of 5" or "X/5" on star containers
    for el in soup.find_all(attrs={"aria-label": True}):
        m = _STAR_RE.search(el.get("aria-label", ""))
        if m:
            try:
                result["star_rating"] = float(m.group(1))
                break
            except ValueError:
                pass

    # ── Review count ──────────────────────────────────────────────────────────
    m = _COUNT_RE.search(page_text)
    if m:
        try:
            result["review_count"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # ── Complaint count ───────────────────────────────────────────────────────
    m = _COMPLAINT_RE.search(page_text)
    if m:
        try:
            result["complaint_count"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    logger.debug(f"BBB scraped {url}: {result}")
    return result
