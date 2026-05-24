import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import cast, Date
from sqlalchemy.orm import Session

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor
from app.models.alerts import Alert, JobRun
from app.models.filings import FilingSnapshot
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
    rank_changes = _build_rank_changes(db, own_firm)
    fc_counts, fc_dist_latest = _compute_filing_counts(db)
    pacer_share = _build_pacer_share(own_firm, fc_counts, fc_dist_latest)
    opportunity_matrix = _build_opportunity_matrix(own_firm, scorecard, pacer_share, fc_counts, fc_dist_latest)
    activity_feed = _build_activity_feed(db, own_firm)

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
        "rank_changes": rank_changes,
        "pacer_share": pacer_share,
        "opportunity_matrix": opportunity_matrix,
        "activity_feed": activity_feed,
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
            fp = json.dumps(s.snapshot_data, sort_keys=True) if s.snapshot_data else str(id(s))
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


def _build_rank_changes(db: Session, own_firm) -> list:
    """Compare most recent ranking date vs ~7 days prior — return list of position changes."""
    if not own_firm:
        return []

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
    if not _latest:
        return []
    rank_date = _latest[0]

    cur_rows = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm.id,
            LocalPackRanking.is_own_firm == True,
            LocalPackRanking.market.in_(MARKET_ORDER),
            cast(LocalPackRanking.scraped_at, Date) == rank_date,
        )
        .all()
    )

    # Prior window: 5–11 days before rank_date to bridge weekend/holiday gaps
    prior_rows = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm.id,
            LocalPackRanking.is_own_firm == True,
            LocalPackRanking.market.in_(MARKET_ORDER),
            cast(LocalPackRanking.scraped_at, Date) >= rank_date - timedelta(days=11),
            cast(LocalPackRanking.scraped_at, Date) <= rank_date - timedelta(days=5),
        )
        .order_by(LocalPackRanking.scraped_at.desc())
        .all()
    )

    prior_by_key: dict = {}
    for r in prior_rows:
        key = (r.keyword, r.market)
        if key not in prior_by_key:
            prior_by_key[key] = r

    if not prior_by_key:
        return []

    def _strip(kw: str, market: str) -> str:
        for city in ["Greensboro", "Winston-Salem", "High Point", "Charlotte",
                     "Salisbury", "Asheville", "Raleigh", "Fayetteville",
                     "Wilmington", "Wilson"]:
            if kw.endswith(" " + city):
                return kw[: -(len(city) + 1)]
        return kw

    drops, gains = [], []
    for r in cur_rows:
        key = (r.keyword, r.market)
        prior = prior_by_key.get(key)
        if not prior:
            continue
        cur_rank  = r.rank_position if r.in_pack else None
        prev_rank = prior.rank_position if prior.in_pack else None
        if cur_rank == prev_rank:
            continue

        kw = _strip(r.keyword or "", r.market)
        city = (r.city or r.market or "").replace("_", " ").title()

        if cur_rank is not None and prev_rank is not None:
            delta = cur_rank - prev_rank  # negative = improved
            entry = {"keyword": kw, "city": city, "cur": cur_rank, "prev": prev_rank,
                     "delta": delta, "type": "improved" if delta < 0 else "dropped"}
            (gains if delta < 0 else drops).append(entry)
        elif cur_rank is None and prev_rank is not None:
            drops.append({"keyword": kw, "city": city, "cur": None, "prev": prev_rank,
                          "delta": None, "type": "dropped_out"})
        elif cur_rank is not None and prev_rank is None:
            gains.append({"keyword": kw, "city": city, "cur": cur_rank, "prev": None,
                          "delta": None, "type": "entered"})

    drops.sort(key=lambda x: abs(x["delta"] or 99), reverse=True)
    gains.sort(key=lambda x: abs(x["delta"] or 99), reverse=True)
    return drops + gains


def _build_activity_feed(db: Session, own_firm) -> list:
    """Chronological timeline of notable competitor events (alerts + review gains)."""
    since_90 = datetime.now(timezone.utc) - timedelta(days=90)
    since_60 = datetime.now(timezone.utc) - timedelta(days=60)
    events = []

    # — Alerts ————————————————————————————————————————————————————————
    _type_meta = {
        "pack_drop":             ("▼", "var(--red-dark)",    "Own firm dropped from 3-pack"),
        "competitor_pack_entry": ("▲", "var(--orange)",      "Competitor entered 3-pack"),
        "review_gap":            ("★", "var(--yellow-dark)", "Review gap alert"),
        "pacer_volume_spike":    ("↑", "var(--purple)",      "PACER volume spike"),
        "pack_convergence":      ("⬆", "var(--orange)",      "Competitor closing on pack position"),
    }
    alerts = (
        db.query(Alert)
        .filter(Alert.triggered_at >= since_90)
        .order_by(Alert.triggered_at.desc())
        .limit(20)
        .all()
    )
    for a in alerts:
        icon, color, default_label = _type_meta.get(a.alert_type, ("•", "var(--muted)", a.alert_type))
        market_str = a.market.replace("_", " ").title() if a.market else ""
        detail = a.detail or {}
        msg    = detail.get("message", "")
        rival  = detail.get("competitor", "")
        summary = (msg or default_label)[:120]
        events.append({
            "ts":         a.triggered_at,
            "event_type": a.alert_type,
            "firm":       rival or "",
            "market":     market_str,
            "summary":    summary,
            "color":      color,
            "icon":       icon,
            "acked":      bool(a.acknowledged_at),
            "source":     "alert",
        })

    # — Competitor review gains ————————————————————————————————————————
    review_snaps = (
        db.query(ReviewSnapshot)
        .filter(
            ReviewSnapshot.snapped_at >= since_60,
            ReviewSnapshot.source == "google",
        )
        .order_by(ReviewSnapshot.competitor_id, ReviewSnapshot.market, ReviewSnapshot.snapped_at.desc())
        .all()
    )
    _by_cm: dict = defaultdict(list)
    for s in review_snaps:
        _by_cm[(s.competitor_id, s.market)].append(s)

    comp_ids = {cid for (cid, _) in _by_cm}
    comp_name_map: dict = {}
    if comp_ids:
        comp_name_map = {
            c.id: c.name
            for c in db.query(Competitor).filter(Competitor.id.in_(comp_ids)).all()
        }

    for (cid, market), snaps in _by_cm.items():
        if len(snaps) < 2:
            continue
        cur, prev = snaps[0], snaps[1]
        if cur.review_count is None or prev.review_count is None:
            continue
        delta = cur.review_count - prev.review_count
        if delta < 3:
            continue
        name = comp_name_map.get(cid, "Unknown")
        market_str = (market or "").replace("_", " ").title()
        events.append({
            "ts":         cur.snapped_at,
            "event_type": "review_gain",
            "firm":       name,
            "market":     market_str,
            "summary":    f"Gained {delta} Google review{'s' if delta != 1 else ''} (now {cur.review_count:,})",
            "color":      "var(--orange)",
            "icon":       "★",
            "acked":      True,
            "source":     "reviews",
        })

    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:30]


def _compute_filing_counts(db: Session) -> tuple:
    """Query FilingSnapshot once and return (counts, dist_latest) for reuse."""
    snaps = db.query(FilingSnapshot).all()
    counts: dict = {}
    for s in snaps:
        key = (s.competitor_id, s.attorney_id, s.district, s.chapter, s.period_start)
        if key not in counts or s.case_count > counts[key]:
            counts[key] = s.case_count
    dist_latest: dict = {}
    for (_cid, _aid, dist, _ch, per) in counts:
        if dist not in dist_latest or per > dist_latest[dist]:
            dist_latest[dist] = per
    return counts, dist_latest


def _build_opportunity_matrix(own_firm, scorecard: list, pacer_share: dict, counts: dict, dist_latest: dict) -> list:
    """Per-market opportunity quadrant: pack strength vs PACER district volume."""
    if not own_firm or not scorecard:
        return []

    dist_totals: dict = {}
    for district, per in dist_latest.items():
        dist_totals[district] = sum(
            count for (_, _, d, _, p), count in counts.items() if d == district and p == per
        )

    _m2d = {
        "greensboro": "MDNC", "winston_salem": "MDNC", "high_point": "MDNC", "salisbury": "MDNC",
        "charlotte": "WDNC", "asheville": "WDNC",
    }
    max_vol = max(dist_totals.values()) if dist_totals else 1
    avg_vol = (sum(dist_totals.values()) / len(dist_totals)) if dist_totals else 0

    matrix = []
    for m in scorecard:
        if m["total"] == 0:
            continue
        district = _m2d.get(m["market"], "MDNC")
        pack_pct = round(m["in_pack"] / m["total"] * 100) if m["total"] > 0 else 0
        dist_total = dist_totals.get(district, 0)
        own_share_pct = pacer_share.get(district)

        high_pack = pack_pct >= 75
        high_vol = dist_total >= avg_vol

        if high_pack and high_vol:
            quadrant, quad_label, quad_color, quad_bg = "defend",   "Defend",   "var(--green-dark)", "var(--green-light)"
        elif not high_pack and high_vol:
            quadrant, quad_label, quad_color, quad_bg = "invest",   "Invest",   "var(--red-dark)",   "var(--red-light)"
        elif high_pack and not high_vol:
            quadrant, quad_label, quad_color, quad_bg = "monitor",  "Monitor",  "var(--accent)",     "var(--accent-light)"
        else:
            quadrant, quad_label, quad_color, quad_bg = "optimize", "Optimize", "var(--yellow-dark)", "var(--yellow-light)"

        vol_score  = dist_total / max_vol if max_vol else 0
        gap_score  = 1 - (pack_pct / 100)
        opportunity_score = round((gap_score * 0.6 + vol_score * 0.4) * 100)

        matrix.append({
            "market":           m["market"],
            "display":          m["display"],
            "district":         district,
            "pack_pct":         pack_pct,
            "in_pack":          m["in_pack"],
            "total_kw":         m["total"],
            "dist_total":       dist_total,
            "own_share_pct":    own_share_pct,
            "opportunity_score": opportunity_score,
            "quadrant":         quadrant,
            "quad_label":       quad_label,
            "quad_color":       quad_color,
            "quad_bg":          quad_bg,
        })

    _qord = {"invest": 0, "optimize": 1, "defend": 2, "monitor": 3}
    matrix.sort(key=lambda x: (_qord.get(x["quadrant"], 9), -x["opportunity_score"]))
    return matrix


def _build_pacer_share(own_firm, counts: dict, dist_latest: dict) -> dict:
    """Most recent month's Duncan Law market share per district (MDNC, WDNC)."""
    if not own_firm or not counts:
        return {}

    result: dict = {}
    for district in ("MDNC", "WDNC"):
        per = dist_latest.get(district)
        if not per:
            continue
        dist_total = own_total = 0
        for (cid, _aid, dist, _ch, period), count in counts.items():
            if dist == district and period == per:
                dist_total += count
                if cid == own_firm.id:
                    own_total += count
        if dist_total > 0:
            result[district] = round(own_total / dist_total * 100, 1)
    return result
