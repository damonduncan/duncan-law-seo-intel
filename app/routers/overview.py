from collections import defaultdict
from datetime import date
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import cast, Date
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor
from app.models.alerts import Alert, JobRun
from app.models.rankings import LocalPackRanking
from app.models.reviews import ReviewSnapshot

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()

MARKET_ORDER = [
    "greensboro", "winston_salem", "high_point",
    "charlotte", "salisbury", "asheville",
]


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    competitors = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).all()
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    unacked_alerts = db.query(Alert).filter(Alert.acknowledged_at == None).count()
    job_runs_raw = db.query(JobRun).order_by(JobRun.started_at.desc()).limit(30).all()
    last_job = job_runs_raw[0] if job_runs_raw else None

    def _fmt_duration(j) -> str:
        if not (j.completed_at and j.started_at):
            return "—"
        secs = max(0, (j.completed_at - j.started_at).total_seconds())
        if secs < 60:
            return f"{int(secs)}s"
        if secs < 3600:
            return f"{int(secs // 60)}m {int(secs % 60)}s"
        return f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"

    job_runs = [{"job": j, "duration": _fmt_duration(j)} for j in job_runs_raw]

    scorecard = _build_scorecard(db, own_firm)
    action_items = _build_action_items(db, own_firm, scorecard, competitors)

    return templates.TemplateResponse("overview.html", {
        "request": request,
        "user": user,
        "own_firm": own_firm,
        "competitors": competitors,
        "unacked_alerts": unacked_alerts,
        "last_job": last_job,
        "job_runs": job_runs,
        "scorecard": scorecard,
        "action_items": action_items,
        "active_page": "dashboard",
    })


def _build_scorecard(db: Session, own_firm) -> list:
    if not own_firm:
        return []

    # Use most recent date with actual own-firm pack data (not hardcoded today)
    _latest = (
        db.query(cast(LocalPackRanking.scraped_at, Date))
        .filter(
            LocalPackRanking.competitor_id == own_firm.id,
            LocalPackRanking.is_own_firm == True,
            LocalPackRanking.in_pack == True,
        )
        .order_by(LocalPackRanking.scraped_at.desc())
        .first()
    )
    rank_date = _latest[0] if _latest else date.today()

    rank_rows = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm.id,
            LocalPackRanking.is_own_firm == True,
            cast(LocalPackRanking.scraped_at, Date) == rank_date,
        )
        .all()
    )
    pack_by_market: dict = {}
    for r in rank_rows:
        m = pack_by_market.setdefault(r.market, {"in_pack": 0, "total": 0})
        m["total"] += 1
        if r.in_pack:
            m["in_pack"] += 1

    # Top competitor per market (#1 in pack on the same date)
    comp_pack_rows = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.in_pack == True,
            LocalPackRanking.is_own_firm == False,
            LocalPackRanking.market.in_(MARKET_ORDER),
            cast(LocalPackRanking.scraped_at, Date) == rank_date,
        )
        .order_by(LocalPackRanking.market, LocalPackRanking.rank_position)
        .all()
    )
    top_comp_id_by_market: dict = {}
    for r in comp_pack_rows:
        if r.market not in top_comp_id_by_market:
            top_comp_id_by_market[r.market] = r.competitor_id

    # Competitor names
    _comp_ids = set(top_comp_id_by_market.values())
    comp_name_map: dict = {}
    if _comp_ids:
        comp_name_map = {
            c.id: c.name
            for c in db.query(Competitor).filter(Competitor.id.in_(_comp_ids)).all()
        }

    # Most recent review count per top competitor (deduplicated across market rows)
    import json as _json
    top_comp_reviews: dict = {}  # competitor_id → review_count
    if _comp_ids:
        comp_snaps = (
            db.query(ReviewSnapshot)
            .filter(
                ReviewSnapshot.competitor_id.in_(_comp_ids),
                ReviewSnapshot.source == "google",
            )
            .order_by(ReviewSnapshot.snapped_at.desc())
            .all()
        )
        seen_fps: dict = {}  # competitor_id → set of fingerprints
        for s in comp_snaps:
            fp = _json.dumps(s.snapshot_data, sort_keys=True) if s.snapshot_data else str(id(s))
            seen_fps.setdefault(s.competitor_id, set())
            if fp not in seen_fps[s.competitor_id]:
                seen_fps[s.competitor_id].add(fp)
                top_comp_reviews[s.competitor_id] = (
                    top_comp_reviews.get(s.competitor_id, 0) + (s.review_count or 0)
                )

    top_comp_by_market: dict = {}
    for market, cid in top_comp_id_by_market.items():
        if cid in comp_name_map:
            top_comp_by_market[market] = {
                "name": comp_name_map[cid],
                "reviews": top_comp_reviews.get(cid),
            }

    # Latest Google review count per own-firm market
    review_snaps = (
        db.query(ReviewSnapshot)
        .filter(
            ReviewSnapshot.competitor_id == own_firm.id,
            ReviewSnapshot.source == "google",
            ReviewSnapshot.market != None,
        )
        .order_by(ReviewSnapshot.snapped_at.desc())
        .all()
    )
    reviews_by_market: dict = {}
    for s in review_snaps:
        if s.market not in reviews_by_market:
            reviews_by_market[s.market] = s.review_count or 0

    scorecard = []
    for market in MARKET_ORDER:
        kw = pack_by_market.get(market, {"in_pack": 0, "total": 0})
        reviews = reviews_by_market.get(market)
        in_pack = kw["in_pack"]
        total = kw["total"]

        if total == 0 and reviews is None:
            status = "no_data"
        elif in_pack == total and total > 0 and (reviews or 0) >= 30:
            status = "strong"
        elif in_pack >= (total // 2 if total else 1) and (reviews or 0) >= 10:
            status = "ok"
        else:
            status = "needs_attention"

        scorecard.append({
            "market":   market,
            "display":  market.replace("_", " ").title(),
            "in_pack":  in_pack,
            "total":    total,
            "reviews":  reviews,
            "status":   status,
            "top_comp": top_comp_by_market.get(market),
        })

    return scorecard


def _build_action_items(db: Session, own_firm, scorecard: list, competitors: list) -> list:
    items = []

    # 1. Markets with zero 3-pack presence despite having ranking data
    for m in scorecard:
        if m["status"] == "needs_attention" and m["total"] > 0 and m["in_pack"] == 0:
            items.append({
                "priority": "high",
                "color": "red",
                "title": f"Not ranking in {m['display']} — 0 of {m['total']} keywords in 3-pack",
                "detail": "Review your Google Business Profile listing and local signals for this market.",
                "link": "/rankings",
                "link_text": "View rankings",
            })

    # 2. Urgent unacknowledged alerts (pack drops / competitor entries)
    urgent_alerts = (
        db.query(Alert)
        .filter(Alert.acknowledged_at == None, Alert.severity == "immediate")
        .order_by(Alert.triggered_at.desc())
        .limit(2)
        .all()
    )
    type_labels = {
        "pack_drop": "Your firm dropped from the 3-pack",
        "competitor_pack_entry": "A competitor entered the 3-pack",
    }
    for a in urgent_alerts:
        label = type_labels.get(a.alert_type, a.alert_type)
        market_str = f" in {a.market.replace('_', ' ').title()}" if a.market else ""
        msg = (a.detail or {}).get("message", "")
        items.append({
            "priority": "high",
            "color": "red",
            "title": f"{label}{market_str}",
            "detail": msg[:120] if msg else "",
            "link": "/alerts",
            "link_text": "View alerts",
        })

    # 3. Markets where own-firm reviews are below 10 (weak social proof)
    for m in scorecard:
        if m["reviews"] is not None and m["reviews"] < 10:
            cnt = m["reviews"]
            items.append({
                "priority": "medium",
                "color": "yellow",
                "title": f"Low review count in {m['display']} ({cnt} review{'s' if cnt != 1 else ''})",
                "detail": "Aim for 30+ reviews to compete reliably in the local pack.",
                "link": "/reviews",
                "link_text": "View reviews",
            })

    # 4. Top competitor gaining review momentum (≥5 new reviews since last snapshot)
    if own_firm:
        comp_snaps = (
            db.query(ReviewSnapshot)
            .filter(
                ReviewSnapshot.source == "google",
                ReviewSnapshot.competitor_id != own_firm.id,
            )
            .order_by(ReviewSnapshot.competitor_id, ReviewSnapshot.snapped_at.desc())
            .all()
        )
        snaps_by_comp: dict = defaultdict(list)
        for s in comp_snaps:
            snaps_by_comp[s.competitor_id].append(s)

        comp_name_map = {c.id: c.name for c in competitors}
        gainers = []
        for cid, snaps in snaps_by_comp.items():
            if (len(snaps) >= 2
                    and snaps[0].review_count is not None
                    and snaps[1].review_count is not None):
                delta = snaps[0].review_count - snaps[1].review_count
                if delta >= 5:
                    gainers.append((delta, cid, comp_name_map.get(cid, cid)))

        gainers.sort(reverse=True)
        for delta, cid, name in gainers[:1]:
            short = name if len(name) <= 30 else name[:29] + "…"
            items.append({
                "priority": "medium",
                "color": "orange",
                "title": f"{short} gaining review momentum (+{delta} recently)",
                "detail": "This competitor is building social proof faster than average — monitor closely.",
                "link": f"/competitors/{cid}",
                "link_text": "View profile",
            })

    # High-priority first, cap at 5
    items.sort(key=lambda x: 0 if x["priority"] == "high" else 1)
    return items[:5]
