import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.discovery import DiscoveryCache
from app.models.competitor import Competitor
from app.models.rankings import LocalPackRanking
from app.models.reviews import ReviewSnapshot
from app.models.base import new_uuid

router = APIRouter()
auth_required = RedirectIfNotAuthenticated()
logger = logging.getLogger(__name__)

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

FIRM_CONTEXT = (
    "Duncan Law LLP is a consumer bankruptcy firm serving Charlotte NC metro. "
    "Markets: Charlotte (WDNC), Salisbury (MDNC), Greensboro (MDNC), "
    "Winston-Salem (MDNC), Asheville (WDNC). "
    "Attorney Damon Duncan founded the firm; Anne Salter joined Oct 2025 "
    "to handle consultations so Damon can focus on signing appointments and filings."
)

SECTION_TASKS = {
    "ppc": (
        "Write 3–5 sentences giving Damon a clear-eyed view of PPC performance. "
        "Which markets are most and least cost-efficient? Is CPL trending in the right direction? "
        "What is the single most actionable recommendation to improve ROI? "
        "Reference specific numbers. Plain prose only, no bullet points or headers."
    ),
    "rankings": (
        "Write 3–5 sentences about current local-pack ranking health. "
        "Which markets and keywords are strongest? Where is the biggest opportunity to improve? "
        "What should the firm prioritize to move up in the pack? "
        "Reference specific positions. Plain prose only, no bullet points or headers."
    ),
    "reviews": (
        "Write 3–5 sentences about the firm's review position. "
        "What momentum exists? Where are we behind competitors and by how much? "
        "Which market needs the most attention and what is the most impactful next step? "
        "Reference specific counts. Plain prose only, no bullet points or headers."
    ),
    "filings": (
        "Write 3–5 sentences about filing volume trends. "
        "Is volume growing, flat, or declining? How does recent volume compare to prior years? "
        "What is the most important strategic observation from this data? "
        "Reference specific numbers. Plain prose only, no bullet points or headers."
    ),
    "overview": (
        "Write 3–5 sentences giving a holistic health snapshot of the firm's market position right now. "
        "What is working well across rankings, reviews, and intake? "
        "What is the single highest-priority thing to address this week? "
        "Be direct and specific. Plain prose only, no bullet points or headers."
    ),
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cache_key(section: str) -> str:
    return f"analysis_{section}"


def _get_cache_row(db: Session, section: str):
    return db.query(DiscoveryCache).filter(
        DiscoveryCache.key == _cache_key(section)
    ).first()


def _read_row(row) -> tuple[str, str]:
    if not row or not row.value:
        return "", ""
    text = row.value if isinstance(row.value, str) else ""
    updated = row.updated_at.strftime("%b %d, %Y") if row.updated_at else ""
    return text, updated


def _save(db: Session, section: str, text: str, row=None) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    if row:
        row.value = text
        row.updated_at = now
    else:
        db.add(DiscoveryCache(
            id=new_uuid(), key=_cache_key(section), value=text, updated_at=now
        ))
    db.commit()
    return text, now.strftime("%b %d, %Y")


def _call_claude(context: str, task: str) -> str:
    from app.config import settings
    import anthropic
    if not settings.anthropic_api_key:
        return ""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": f"{FIRM_CONTEXT}\n\n{context}\n\n{task}"}],
    )
    return resp.content[0].text.strip()


# ── Public helper — page routers call this to pre-load cached analysis ─────────

def load_cached_analysis(db: Session, section: str) -> tuple[str, str]:
    return _read_row(_get_cache_row(db, section))


# ── Section data fetchers ──────────────────────────────────────────────────────

def _ppc_context(db: Session) -> str:
    row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "ppc_monthly_data"
    ).first()
    data = row.value if row else []
    if isinstance(data, str):
        data = json.loads(data)
    if not data:
        return "No PPC data available yet."

    months = sorted(data, key=lambda x: (x.get("year", 0), x.get("month", 0)))
    recent = months[-6:] if len(months) >= 6 else months

    lines = ["PPC PERFORMANCE — last 6 months:"]
    for m in recent:
        yr, mo = m.get("year"), m.get("month")
        t = m.get("total", {})
        label = f"{MONTH_NAMES[mo - 1]} {yr}" if mo else str(yr)
        leads = t.get("leads", "?")
        spend = t.get("spend", 0) or 0
        cpl   = t.get("cpl", 0) or 0
        lines.append(f"  {label}: leads={leads}, spend=${spend:,.0f}, CPL=${cpl:.2f}")

    latest = recent[-1]
    yr, mo = latest.get("year"), latest.get("month")
    label = f"{MONTH_NAMES[mo - 1]} {yr}" if mo else ""
    lines.append(f"\nMOST RECENT MONTH ({label}) BY MARKET:")
    for mkt in ["charlotte", "salisbury", "greensboro", "winston_salem", "asheville"]:
        ms = latest.get(mkt, {})
        leads = ms.get("leads")
        if leads:
            cpl = ms.get("cpl", 0) or 0
            lines.append(f"  {mkt.replace('_', ' ').title()}: leads={leads}, CPL=${cpl:.2f}")

    total_leads = sum(m.get("total", {}).get("leads", 0) or 0 for m in months)
    total_spend = sum(m.get("total", {}).get("spend", 0) or 0 for m in months)
    avg_cpl = round(total_spend / total_leads, 2) if total_leads else 0
    lines.append(
        f"\nALL-TIME TOTALS ({len(months)} months): "
        f"{total_leads} leads, ${total_spend:,.0f} spend, ${avg_cpl:.2f} avg CPL"
    )
    return "\n".join(lines)


def _rankings_context(db: Session) -> str:
    own = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    if not own:
        return "No firm data found."

    subq = (
        db.query(
            LocalPackRanking.keyword,
            LocalPackRanking.city,
            func.max(LocalPackRanking.scraped_at).label("max_dt"),
        )
        .group_by(LocalPackRanking.keyword, LocalPackRanking.city)
        .subquery()
    )

    latest = (
        db.query(LocalPackRanking)
        .join(
            subq,
            (LocalPackRanking.keyword == subq.c.keyword)
            & (LocalPackRanking.city == subq.c.city)
            & (LocalPackRanking.scraped_at == subq.c.max_dt),
        )
        .all()
    )

    own_ranks = sorted(
        [r for r in latest if r.competitor_id == own.id],
        key=lambda r: (r.city or "", r.keyword or ""),
    )

    lines = ["CURRENT LOCAL PACK POSITIONS (Duncan Law):"]
    if own_ranks:
        for r in own_ranks:
            rank_str = f"#{r.rank}" if r.rank else "not in pack"
            lines.append(f"  {r.city} / {r.keyword}: {rank_str}")
    else:
        lines.append("  No rankings found yet.")

    # Top competitors for comparison
    comp_ranks = {}
    for r in latest:
        if r.competitor_id != own.id and r.rank and r.rank <= 3:
            key = (r.city, r.keyword)
            if key not in comp_ranks:
                comp_ranks[key] = []
            comp = db.query(Competitor).filter(Competitor.id == r.competitor_id).first()
            if comp:
                comp_ranks[key].append(f"{comp.name} #{r.rank}")

    if comp_ranks:
        lines.append("\nTOP-3 COMPETITORS BY MARKET/KEYWORD (sample):")
        for (city, kw), comps in list(comp_ranks.items())[:6]:
            lines.append(f"  {city} / {kw}: {', '.join(comps[:2])}")

    return "\n".join(lines)


def _reviews_context(db: Session) -> str:
    own = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    if not own:
        return "No firm data found."

    subq = (
        db.query(
            ReviewSnapshot.market,
            ReviewSnapshot.source,
            func.max(ReviewSnapshot.scraped_at).label("max_dt"),
        )
        .filter(ReviewSnapshot.competitor_id == own.id)
        .group_by(ReviewSnapshot.market, ReviewSnapshot.source)
        .subquery()
    )

    current = (
        db.query(ReviewSnapshot)
        .join(
            subq,
            (ReviewSnapshot.competitor_id == own.id)
            & (ReviewSnapshot.market == subq.c.market)
            & (ReviewSnapshot.source == subq.c.source)
            & (ReviewSnapshot.scraped_at == subq.c.max_dt),
        )
        .all()
    )

    thirty_ago = datetime.now(timezone.utc) - timedelta(days=35)
    old_snaps = (
        db.query(ReviewSnapshot)
        .filter(
            ReviewSnapshot.competitor_id == own.id,
            ReviewSnapshot.scraped_at < thirty_ago,
        )
        .order_by(ReviewSnapshot.scraped_at.desc())
        .limit(30)
        .all()
    )
    old_by_key = {}
    for s in old_snaps:
        key = (s.market, s.source)
        if key not in old_by_key:
            old_by_key[key] = s.total_reviews

    lines = ["DUNCAN LAW REVIEW COUNTS (current vs. ~30 days ago):"]
    for s in sorted(current, key=lambda x: x.market or ""):
        key = (s.market, s.source)
        old = old_by_key.get(key)
        delta = f" (+{s.total_reviews - old} in ~30d)" if old is not None and s.total_reviews > old else ""
        lines.append(f"  {s.market or '?'} / {s.source}: {s.total_reviews}{delta}")

    # Top competitor totals
    comps = (
        db.query(Competitor)
        .filter(Competitor.is_own_firm == False)
        .limit(8)
        .all()
    )
    comp_ids = [c.id for c in comps]
    comp_snaps = (
        db.query(ReviewSnapshot)
        .filter(ReviewSnapshot.competitor_id.in_(comp_ids))
        .order_by(ReviewSnapshot.scraped_at.desc())
        .limit(60)
        .all()
    )
    comp_totals = {}
    for s in comp_snaps:
        if s.competitor_id not in comp_totals and s.total_reviews:
            comp = next((c for c in comps if c.id == s.competitor_id), None)
            if comp:
                comp_totals[s.competitor_id] = {"name": comp.name, "count": s.total_reviews}

    if comp_totals:
        lines.append("\nCOMPETITOR REVIEW COUNTS (most recent snapshot):")
        for v in sorted(comp_totals.values(), key=lambda x: -x["count"])[:6]:
            lines.append(f"  {v['name']}: {v['count']}")

    return "\n".join(lines)


def _filings_context(db: Session) -> str:
    row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "duncan_law_filing_history"
    ).first()
    data = row.value if row else {}
    if isinstance(data, str):
        data = json.loads(data)
    annual = data.get("annual", [])
    if not annual:
        return "No filing data available yet."

    lines = ["FILING HISTORY (Duncan Law LLP):"]
    for yr_data in sorted(annual, key=lambda x: x.get("year", 0)):
        yr = yr_data.get("year")
        total = yr_data.get("total", 0)
        monthly = yr_data.get("monthly") or []
        line = f"  {yr}: {total} cases"
        if monthly and yr >= 2024:
            month_parts = [
                f"{MONTH_NAMES[i]}={v}"
                for i, v in enumerate(monthly)
                if v is not None and v > 0
            ]
            if month_parts:
                line += f" ({', '.join(month_parts)})"
        lines.append(line)

    # Competitor filings if available
    comp_data = data.get("competitors", {})
    if comp_data:
        lines.append("\nCOMPETITOR FILINGS (most recent year available):")
        for name, years in list(comp_data.items())[:5]:
            latest = max(years.items(), key=lambda x: x[0]) if years else None
            if latest:
                lines.append(f"  {name}: {latest[1]} cases in {latest[0]}")

    return "\n".join(lines)


def _overview_context(db: Session) -> str:
    own = db.query(Competitor).filter(Competitor.is_own_firm == True).first()

    lines = [f"FIRM: {own.name if own else 'Duncan Law LLP'}"]

    # Own rankings snapshot
    if own:
        seven_ago = datetime.now(timezone.utc) - timedelta(days=7)
        subq = (
            db.query(
                LocalPackRanking.keyword,
                LocalPackRanking.city,
                func.max(LocalPackRanking.scraped_at).label("max_dt"),
            )
            .filter(
                LocalPackRanking.competitor_id == own.id,
                LocalPackRanking.scraped_at >= seven_ago,
            )
            .group_by(LocalPackRanking.keyword, LocalPackRanking.city)
            .subquery()
        )
        recent_ranks = (
            db.query(LocalPackRanking)
            .join(
                subq,
                (LocalPackRanking.keyword == subq.c.keyword)
                & (LocalPackRanking.city == subq.c.city)
                & (LocalPackRanking.scraped_at == subq.c.max_dt),
            )
            .all()
        )
        if recent_ranks:
            lines.append("\nCURRENT RANKINGS:")
            for r in sorted(recent_ranks, key=lambda x: (x.city or "", x.keyword or "")):
                lines.append(f"  {r.city} / {r.keyword}: #{r.rank}")

        # Review totals
        rev_subq = (
            db.query(
                ReviewSnapshot.market,
                ReviewSnapshot.source,
                func.max(ReviewSnapshot.scraped_at).label("max_dt"),
            )
            .filter(ReviewSnapshot.competitor_id == own.id)
            .group_by(ReviewSnapshot.market, ReviewSnapshot.source)
            .subquery()
        )
        own_snaps = (
            db.query(ReviewSnapshot)
            .join(
                rev_subq,
                (ReviewSnapshot.competitor_id == own.id)
                & (ReviewSnapshot.market == rev_subq.c.market)
                & (ReviewSnapshot.source == rev_subq.c.source)
                & (ReviewSnapshot.scraped_at == rev_subq.c.max_dt),
            )
            .all()
        )
        if own_snaps:
            lines.append("\nREVIEW COUNTS (own firm):")
            for s in sorted(own_snaps, key=lambda x: x.market or ""):
                lines.append(f"  {s.market} / {s.source}: {s.total_reviews}")

    # PPC summary (last month)
    ppc_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "ppc_monthly_data"
    ).first()
    ppc_data = ppc_row.value if ppc_row else []
    if isinstance(ppc_data, str):
        ppc_data = json.loads(ppc_data)
    if ppc_data:
        latest_ppc = sorted(
            ppc_data, key=lambda x: (x.get("year", 0), x.get("month", 0))
        )[-1]
        t = latest_ppc.get("total", {})
        mo = latest_ppc.get("month")
        yr = latest_ppc.get("year")
        label = f"{MONTH_NAMES[mo - 1]} {yr}" if mo else str(yr)
        lines.append(
            f"\nPPC (most recent month — {label}): "
            f"leads={t.get('leads', '?')}, CPL=${t.get('cpl', 0):.2f}"
        )

    # Filing pace
    filing_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "duncan_law_filing_history"
    ).first()
    filing_data = filing_row.value if filing_row else {}
    if isinstance(filing_data, str):
        filing_data = json.loads(filing_data)
    annual = filing_data.get("annual", [])
    if annual:
        latest_yr = max(annual, key=lambda x: x.get("year", 0))
        lines.append(
            f"\nFILINGS ({latest_yr.get('year')}): "
            f"{latest_yr.get('total', 0)} cases YTD"
        )

    return "\n".join(lines)


SECTION_FETCHERS = {
    "ppc":      _ppc_context,
    "rankings": _rankings_context,
    "reviews":  _reviews_context,
    "filings":  _filings_context,
    "overview": _overview_context,
}


# ── Metrics snapshots (recorded alongside each history entry) ──────────────────

def _snap_ppc(db: Session) -> dict:
    row = db.query(DiscoveryCache).filter(DiscoveryCache.key == "ppc_monthly_data").first()
    data = row.value if row else []
    if isinstance(data, str):
        data = json.loads(data)
    if not data:
        return {}
    recent = sorted(data, key=lambda x: (x.get("year", 0), x.get("month", 0)))[-3:]
    total_leads = sum(m.get("total", {}).get("leads", 0) or 0 for m in recent)
    total_spend = sum(m.get("total", {}).get("spend", 0) or 0 for m in recent)
    avg_cpl = round(total_spend / total_leads, 2) if total_leads else None
    latest = recent[-1] if recent else {}
    mo, yr = latest.get("month"), latest.get("year")
    period = f"{MONTH_NAMES[mo - 1]} {yr}" if mo else str(yr or "")
    return {"period": f"Last 3 mo through {period}", "leads_3mo": total_leads, "avg_cpl_3mo": avg_cpl}


def _snap_rankings(db: Session) -> dict:
    own = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    if not own:
        return {}
    subq = (
        db.query(LocalPackRanking.keyword, LocalPackRanking.city,
                 func.max(LocalPackRanking.scraped_at).label("max_dt"))
        .group_by(LocalPackRanking.keyword, LocalPackRanking.city).subquery()
    )
    rows = (
        db.query(LocalPackRanking)
        .join(subq, (LocalPackRanking.keyword == subq.c.keyword)
              & (LocalPackRanking.city == subq.c.city)
              & (LocalPackRanking.scraped_at == subq.c.max_dt))
        .filter(LocalPackRanking.competitor_id == own.id).all()
    )
    return {"positions": {f"{r.city}/{r.keyword}": r.rank for r in rows if r.rank}}


def _snap_reviews(db: Session) -> dict:
    own = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    if not own:
        return {}
    subq = (
        db.query(ReviewSnapshot.market, ReviewSnapshot.source,
                 func.max(ReviewSnapshot.scraped_at).label("max_dt"))
        .filter(ReviewSnapshot.competitor_id == own.id)
        .group_by(ReviewSnapshot.market, ReviewSnapshot.source).subquery()
    )
    snaps = (
        db.query(ReviewSnapshot)
        .join(subq, (ReviewSnapshot.competitor_id == own.id)
              & (ReviewSnapshot.market == subq.c.market)
              & (ReviewSnapshot.source == subq.c.source)
              & (ReviewSnapshot.scraped_at == subq.c.max_dt)).all()
    )
    markets = {s.market: s.total_reviews for s in snaps
               if s.source and s.source.lower() == "google" and s.total_reviews}
    return {"markets": markets, "total_google": sum(markets.values())}


def _snap_filings(db: Session) -> dict:
    row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "duncan_law_filing_history"
    ).first()
    data = row.value if row else {}
    if isinstance(data, str):
        data = json.loads(data)
    annual = data.get("annual", [])
    if not annual:
        return {}
    latest = max(annual, key=lambda x: x.get("year", 0))
    monthly = {MONTH_NAMES[i]: v for i, v in enumerate(latest.get("monthly") or [])
               if v is not None and v > 0}
    return {"year": latest.get("year"), "ytd_cases": latest.get("total", 0), "monthly": monthly}


def _snap_overview(db: Session) -> dict:
    snap: dict = {}
    own = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    if own:
        subq = (
            db.query(LocalPackRanking.keyword, LocalPackRanking.city,
                     func.max(LocalPackRanking.scraped_at).label("max_dt"))
            .filter(LocalPackRanking.competitor_id == own.id)
            .group_by(LocalPackRanking.keyword, LocalPackRanking.city).subquery()
        )
        ranks = (
            db.query(LocalPackRanking)
            .join(subq, (LocalPackRanking.keyword == subq.c.keyword)
                  & (LocalPackRanking.city == subq.c.city)
                  & (LocalPackRanking.scraped_at == subq.c.max_dt)).all()
        )
        snap["rankings"] = {f"{r.city}/{r.keyword}": r.rank for r in ranks if r.rank}
        rev_subq = (
            db.query(ReviewSnapshot.market, ReviewSnapshot.source,
                     func.max(ReviewSnapshot.scraped_at).label("max_dt"))
            .filter(ReviewSnapshot.competitor_id == own.id)
            .group_by(ReviewSnapshot.market, ReviewSnapshot.source).subquery()
        )
        rev_snaps = (
            db.query(ReviewSnapshot)
            .join(rev_subq, (ReviewSnapshot.competitor_id == own.id)
                  & (ReviewSnapshot.market == rev_subq.c.market)
                  & (ReviewSnapshot.source == rev_subq.c.source)
                  & (ReviewSnapshot.scraped_at == rev_subq.c.max_dt)).all()
        )
        snap["reviews_total"] = sum(s.total_reviews or 0 for s in rev_snaps)
    ppc_row = db.query(DiscoveryCache).filter(DiscoveryCache.key == "ppc_monthly_data").first()
    ppc_data = ppc_row.value if ppc_row else []
    if isinstance(ppc_data, str):
        ppc_data = json.loads(ppc_data)
    if ppc_data:
        lp = sorted(ppc_data, key=lambda x: (x.get("year", 0), x.get("month", 0)))[-1]
        snap["ppc_leads_last_month"] = lp.get("total", {}).get("leads")
        snap["ppc_cpl_last_month"] = lp.get("total", {}).get("cpl")
    return snap


SECTION_SNAPSHOTS = {
    "ppc":      _snap_ppc,
    "rankings": _snap_rankings,
    "reviews":  _snap_reviews,
    "filings":  _snap_filings,
    "overview": _snap_overview,
}

HISTORY_CAP = 52  # ~1 year of weekly entries


def _history_key(section: str) -> str:
    return f"analysis_history_{section}"


def _append_history(db: Session, section: str, text: str, metrics: dict) -> None:
    """Prepend new entry to the history list (newest first), capped at HISTORY_CAP."""
    now = datetime.now(timezone.utc)
    entry = {
        "text": text,
        "generated_at": now.isoformat(),
        "generated_at_label": now.strftime("%b %d, %Y"),
        "metrics": metrics,
    }
    hist_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == _history_key(section)
    ).first()
    existing: list = []
    if hist_row:
        v = hist_row.value
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = []
        existing = v if isinstance(v, list) else []

    entries = ([entry] + existing)[:HISTORY_CAP]

    if hist_row:
        hist_row.value = entries
        hist_row.updated_at = now
    else:
        db.add(DiscoveryCache(
            id=new_uuid(), key=_history_key(section), value=entries, updated_at=now
        ))
    db.commit()


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/analyze/{section}/cached")
async def get_cached(
    section: str,
    db: Session = Depends(get_db),
    user: dict = Depends(auth_required),
):
    if section not in SECTION_FETCHERS:
        return {"text": "", "updated": ""}
    text, updated = _read_row(_get_cache_row(db, section))
    return {"text": text, "updated": updated}


@router.get("/analyze/{section}/history")
async def get_history(
    section: str,
    db: Session = Depends(get_db),
    user: dict = Depends(auth_required),
):
    hist_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == _history_key(section)
    ).first()
    if not hist_row or not hist_row.value:
        return {"entries": []}
    entries = hist_row.value
    if isinstance(entries, str):
        try:
            entries = json.loads(entries)
        except Exception:
            entries = []
    return {"entries": entries if isinstance(entries, list) else []}


@router.post("/analyze/{section}")
async def analyze_section(
    section: str,
    db: Session = Depends(get_db),
    user: dict = Depends(auth_required),
):
    if section not in SECTION_FETCHERS:
        return JSONResponse({"error": f"Unknown section: {section}"}, status_code=404)

    row = _get_cache_row(db, section)
    try:
        context = SECTION_FETCHERS[section](db)
        text = _call_claude(context, SECTION_TASKS[section])
        text, updated = _save(db, section, text, row)

        # Snapshot current metrics and persist to history
        try:
            metrics = SECTION_SNAPSHOTS[section](db)
        except Exception as snap_err:
            logger.warning(f"Metrics snapshot failed [{section}]: {snap_err}")
            metrics = {}
        _append_history(db, section, text, metrics)

        return {"text": text, "updated": updated}
    except Exception as e:
        logger.error(f"Analysis failed [{section}]: {e}", exc_info=True)
        cached_text, cached_updated = _read_row(row)
        if cached_text:
            return {"text": cached_text, "updated": cached_updated, "cached": True}
        return JSONResponse({"error": "Analysis failed. Please try again."}, status_code=500)
