"""Weekly email digest — Phase 5.

Sends a Monday morning intelligence summary to damonduncan@duncanlawonline.com
via Resend covering:
  • Duncan Law's current Google 3-pack positions across all 6 markets
  • Review counts by market with week-over-week deltas
  • District review intelligence (MDNC / WDNC / EDNC competitive standings)
  • Top competitor review gainers since last collection
  • PACER filing standings per district (most recent monthly collection)
  • All unacknowledged alerts from the past week
  • Quick links back to the full dashboard
"""
import json
import logging
from collections import Counter, defaultdict
from datetime import date, timedelta, datetime, timezone

from sqlalchemy import cast, Date
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alerts import Alert, DigestLog
from app.models.base import new_uuid
from app.models.competitor import Competitor, CompetitorLocation
from app.models.filings import FilingSnapshot
from app.models.rankings import LocalPackRanking
from app.models.reviews import ReviewSnapshot
from app.constants import MARKET_TO_DISTRICT

logger = logging.getLogger(__name__)

MARKET_DISPLAY = {
    "greensboro":    "Greensboro",
    "winston_salem": "Winston-Salem",
    "high_point":    "High Point",
    "charlotte":     "Charlotte",
    "salisbury":     "Salisbury",
    "asheville":     "Asheville",
}
MARKET_ORDER = list(MARKET_DISPLAY.keys())
OWN_MDNC = frozenset(["greensboro", "winston_salem", "high_point", "salisbury"])
OWN_WDNC = frozenset(["charlotte", "asheville"])
DISTRICT_ORDER = ["MDNC", "WDNC", "EDNC"]
DISTRICT_LABELS = {
    "MDNC": "Middle District NC",
    "WDNC": "Western District NC",
    "EDNC": "Eastern District NC",
}
DISTRICT_COLORS = {
    "MDNC": ("#dbeafe", "#1e3a8a", "#2563eb"),
    "WDNC": ("#ede9fe", "#4c1d95", "#7c3aed"),
    "EDNC": ("#ffedd5", "#7c2d12", "#ea580c"),
}


def build_and_send_digest(db: Session) -> None:
    """Build the weekly digest and send via Resend. Logs result to digest_log."""
    if not settings.resend_api_key:
        raise ValueError("RESEND_API_KEY not configured — add it to Railway environment variables")

    today    = date.today()
    week_str = today.strftime("%B %d, %Y")
    subject  = f"Duncan Law SEO Intelligence — Week of {week_str}"

    ctx  = _gather_data(db)
    from app.services.ai_recommendations import generate_recommendations, generate_narrative
    ctx["ai_recommendations"] = generate_recommendations(ctx)
    ctx["ai_narrative"]        = generate_narrative(ctx)
    html = _build_html(ctx, week_str)

    log = DigestLog(
        id=new_uuid(),
        sent_at=datetime.now(timezone.utc),
        recipient=settings.digest_recipient,
        subject=subject,
        status="failed",
    )
    db.add(log)

    try:
        import resend as resend_sdk
        resend_sdk.api_key = settings.resend_api_key
        result = resend_sdk.Emails.send({
            "from":    settings.resend_from_address,
            "to":      settings.digest_recipient,
            "subject": subject,
            "html":    html,
        })
        log.status            = "sent"
        log.resend_message_id = result.get("id") if isinstance(result, dict) else str(result)
        logger.info(f"Weekly digest sent to {settings.digest_recipient}")
    except Exception as e:
        log.error_detail = str(e)
        logger.error(f"Digest send failed: {e}")
    finally:
        db.commit()


# ── Data gathering ────────────────────────────────────────────────────────────

def _gather_data(db: Session) -> dict:
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    own_id   = own_firm.id if own_firm else None

    # Latest own-firm rankings — most recent date available
    latest_date = None
    if own_id:
        row = (
            db.query(LocalPackRanking.scraped_at)
            .filter(LocalPackRanking.competitor_id == own_id)
            .order_by(LocalPackRanking.scraped_at.desc())
            .first()
        )
        if row:
            latest_date = row[0].date()

    rankings_by_market: dict = {}
    if own_id and latest_date:
        rows = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.competitor_id == own_id,
                LocalPackRanking.is_own_firm == True,
                cast(LocalPackRanking.scraped_at, Date) == latest_date,
            )
            .all()
        )
        for r in rows:
            m = rankings_by_market.setdefault(
                r.market, {"in_pack": 0, "total": 0, "gaps": [], "positions": []}
            )
            m["total"] += 1
            if r.in_pack:
                m["in_pack"] += 1
                if r.rank_position:
                    m["positions"].append(r.rank_position)
            else:
                m["gaps"].append(r.keyword)

    # Prior-week rankings for rank direction arrows (5–11 days before latest_date)
    prior_positions_by_market: dict = {}
    if own_id and latest_date:
        prior_row = (
            db.query(LocalPackRanking.scraped_at)
            .filter(
                LocalPackRanking.competitor_id == own_id,
                LocalPackRanking.is_own_firm == True,
                cast(LocalPackRanking.scraped_at, Date) >= latest_date - timedelta(days=11),
                cast(LocalPackRanking.scraped_at, Date) < latest_date,
            )
            .order_by(LocalPackRanking.scraped_at.desc())
            .first()
        )
        if prior_row:
            prior_date = prior_row[0].date()
            for r in (
                db.query(LocalPackRanking)
                .filter(
                    LocalPackRanking.competitor_id == own_id,
                    LocalPackRanking.is_own_firm == True,
                    cast(LocalPackRanking.scraped_at, Date) == prior_date,
                )
                .all()
            ):
                if r.in_pack and r.rank_position:
                    prior_positions_by_market.setdefault(r.market, []).append(r.rank_position)

    rank_direction: dict = {}
    for market, data in rankings_by_market.items():
        cur = data.get("positions", [])
        prior = prior_positions_by_market.get(market, [])
        if cur and prior:
            delta = sum(prior) / len(prior) - sum(cur) / len(cur)
            rank_direction[market] = "up" if delta > 0.3 else ("down" if delta < -0.3 else "same")

    # Latest own-firm review snapshot per market + week-over-week deltas
    reviews_by_market: dict = {}
    own_review_deltas: dict = {}
    own_review_rates: dict = {}
    if own_id:
        snaps = (
            db.query(ReviewSnapshot)
            .filter(
                ReviewSnapshot.competitor_id == own_id,
                ReviewSnapshot.source == "google",
                ReviewSnapshot.market != None,
            )
            .order_by(ReviewSnapshot.snapped_at.desc())
            .all()
        )
        # Group by market — list is newest-first from the query
        snap_by_market: dict = {}
        for s in snaps:
            snap_by_market.setdefault(s.market, []).append(s)
        for market, msnaps in snap_by_market.items():
            reviews_by_market[market] = {
                "rating":       float(msnaps[0].rating) if msnaps[0].rating else None,
                "review_count": msnaps[0].review_count or 0,
            }
            if (len(msnaps) >= 2
                    and msnaps[0].review_count is not None
                    and msnaps[1].review_count is not None):
                own_review_deltas[market] = msnaps[0].review_count - msnaps[1].review_count
            own_review_rates[market] = _rolling_weekly_rate(msnaps)

    # Competitor review velocity leaders (top gainers since last collection)
    velocity_leaders: list = []
    competitors_all = (
        db.query(Competitor)
        .filter(Competitor.is_own_firm == False, Competitor.active == True)
        .all()
    )
    for c in competitors_all:
        pair = (
            db.query(ReviewSnapshot)
            .filter(ReviewSnapshot.competitor_id == c.id, ReviewSnapshot.source == "google")
            .order_by(ReviewSnapshot.snapped_at.desc())
            .limit(2)
            .all()
        )
        if (len(pair) >= 2
                and pair[0].review_count is not None
                and pair[1].review_count is not None):
            delta = pair[0].review_count - pair[1].review_count
            # Cap at 50 — larger deltas are scraper artifacts (new GBP location
            # picked up in one run, not actual reviews gained in a week)
            if 0 < delta <= 50:
                velocity_leaders.append({
                    "name":  c.name,
                    "delta": delta,
                    "total": pair[0].review_count,
                })
    velocity_leaders.sort(key=lambda r: r["delta"], reverse=True)
    velocity_leaders = velocity_leaders[:3]

    # Pack entry alerts this week — grouped for dedicated digest section
    since_7d = datetime.now(timezone.utc) - timedelta(days=7)
    pack_entry_alerts = (
        db.query(Alert)
        .filter(
            Alert.alert_type == "competitor_pack_entry",
            Alert.triggered_at >= since_7d,
        )
        .order_by(Alert.market, Alert.triggered_at.desc())
        .all()
    )
    pack_entries_by_market: dict = defaultdict(list)
    _seen_pack_entries: dict = defaultdict(set)
    for a in pack_entry_alerts:
        comp_name = a.detail.get("competitor_name", "Unknown") if a.detail else "Unknown"
        keyword   = a.keyword or (a.detail.get("keyword", "—") if a.detail else "—")
        entry_key = (comp_name, keyword)
        if entry_key not in _seen_pack_entries[a.market]:
            _seen_pack_entries[a.market].add(entry_key)
            pack_entries_by_market[a.market].append({
                "competitor": comp_name,
                "keyword":    keyword,
                "position":   a.detail.get("position") if a.detail else None,
            })

    # Unacknowledged alerts (excluding pack entries — covered in their own section)
    open_alerts = (
        db.query(Alert)
        .filter(
            Alert.acknowledged_at == None,
            Alert.alert_type != "competitor_pack_entry",
        )
        .order_by(Alert.triggered_at.desc())
        .limit(10)
        .all()
    )

    priority_action = _generate_priority(rankings_by_market, reviews_by_market)

    # ── District review intelligence ──────────────────────────────────────────
    # Map each competitor_id to its primary district via CompetitorLocation
    all_locs = db.query(CompetitorLocation).all()
    _loc_markets: dict = defaultdict(set)
    for loc in all_locs:
        if loc.market:
            _loc_markets[loc.competitor_id].add(loc.market)
    comp_district_map: dict = {}
    for cid, markets in _loc_markets.items():
        cnts = Counter(MARKET_TO_DISTRICT[m] for m in markets if m in MARKET_TO_DISTRICT)
        if cnts:
            comp_district_map[cid] = cnts.most_common(1)[0][0]

    # Recent snapshots for all firms (last 60 days)
    since60 = datetime.now(timezone.utc) - timedelta(days=60)
    all_snaps_recent = (
        db.query(ReviewSnapshot)
        .filter(ReviewSnapshot.source == "google", ReviewSnapshot.snapped_at >= since60)
        .order_by(ReviewSnapshot.snapped_at.desc())
        .all()
    )

    def _dedup_snaps(snaps):
        seen, out = set(), []
        for s in snaps:
            fp = json.dumps(s.snapshot_data, sort_keys=True) if s.snapshot_data else str(id(s))
            if fp not in seen:
                seen.add(fp)
                out.append(s)
        return out

    all_by_comp: dict = defaultdict(list)
    comp_snaps_by_market: dict = defaultdict(lambda: defaultdict(list))
    for s in all_snaps_recent:
        all_by_comp[s.competitor_id].append(s)
        if s.competitor_id != own_id and s.market and s.market in MARKET_DISPLAY:
            comp_snaps_by_market[s.market][s.competitor_id].append(s)

    district_review_standings: dict = {d: [] for d in DISTRICT_ORDER}

    # Own firm — MDNC and WDNC totals from their respective office markets
    if own_id and own_id in all_by_comp:
        own_by_mkt: dict = defaultdict(list)
        for s in all_by_comp[own_id]:
            if s.market:
                own_by_mkt[s.market].append(s)
        own_current = {m: snaps[0] for m, snaps in own_by_mkt.items() if snaps}
        mdnc_c = [s.review_count for m, s in own_current.items() if m in OWN_MDNC and s.review_count]
        wdnc_c = [s.review_count for m, s in own_current.items() if m in OWN_WDNC and s.review_count]
        if mdnc_c:
            district_review_standings["MDNC"].append({"name": "Duncan Law", "count": sum(mdnc_c), "is_own": True})
        if wdnc_c:
            district_review_standings["WDNC"].append({"name": "Duncan Law", "count": sum(wdnc_c), "is_own": True})

    # Competitors
    for c in competitors_all:
        dist = comp_district_map.get(c.id)
        if not dist or dist not in district_review_standings:
            continue
        snaps = all_by_comp.get(c.id, [])
        if not snaps:
            continue
        by_mkt: dict = defaultdict(list)
        for s in snaps:
            if s.market:
                by_mkt[s.market].append(s)
        current = [msnaps[0] for msnaps in by_mkt.values() if msnaps]
        deduped = _dedup_snaps(current)
        counts = [s.review_count for s in deduped if s.review_count is not None]
        if counts:
            district_review_standings[dist].append({"name": c.name, "count": sum(counts), "is_own": False})

    for dist in DISTRICT_ORDER:
        district_review_standings[dist].sort(key=lambda r: r["count"], reverse=True)

    # ── PACER district standings ───────────────────────────────────────────────
    all_filing_snaps = db.query(FilingSnapshot).all()
    _f_deduped: dict = {}
    for s in all_filing_snaps:
        key = (s.competitor_id, s.attorney_id, s.district, s.chapter, s.period_start)
        if key not in _f_deduped or s.case_count > _f_deduped[key]:
            _f_deduped[key] = s.case_count

    _dist_latest: dict = {}
    for (cid, aid, dist, chap, per), cnt in _f_deduped.items():
        if dist and per and (dist not in _dist_latest or per > _dist_latest[dist]):
            _dist_latest[dist] = per

    _comp_dist_counts: dict = defaultdict(lambda: defaultdict(int))
    for (cid, aid, dist, chap, per), cnt in _f_deduped.items():
        if dist and _dist_latest.get(dist) == per:
            _comp_dist_counts[cid][dist] += cnt

    _comp_name_map = {c.id: c.name for c in competitors_all}
    if own_firm:
        _comp_name_map[own_id] = own_firm.name

    # Market velocity: own rate vs top rival rate per market
    market_velocity: list = []
    for market in MARKET_ORDER:
        own_count = reviews_by_market.get(market, {}).get("review_count")
        if own_count is None:
            continue
        own_delta = own_review_rates.get(market) or 0
        top_rival_name = None
        top_rival_count = 0
        top_rival_delta = 0
        for comp_id, snaps in comp_snaps_by_market.get(market, {}).items():
            if not snaps or snaps[0].review_count is None:
                continue
            cnt = snaps[0].review_count
            if cnt > top_rival_count:
                top_rival_count = cnt
                top_rival_name = _comp_name_map.get(comp_id, "Unknown")
                top_rival_delta = _rolling_weekly_rate(snaps)
        if not top_rival_name:
            continue
        gap = top_rival_count - own_count
        rate_diff = own_delta - top_rival_delta
        if gap <= 0:
            proj_text, proj_color = "Leading", "green"
        elif rate_diff > 0:
            weeks = max(1, round(gap / rate_diff))
            proj_text, proj_color = f"~{weeks}w to close", "blue"
        elif rate_diff < 0:
            proj_text, proj_color = "Widening", "red"
        else:
            proj_text, proj_color = "Static", "gray"
        market_velocity.append({
            "display":     MARKET_DISPLAY.get(market, market),
            "own_count":   own_count,
            "own_delta":   own_delta,
            "rival_name":  (top_rival_name[:28] + "…") if len(top_rival_name) > 28 else top_rival_name,
            "rival_count": top_rival_count,
            "rival_delta": top_rival_delta,
            "gap":         gap,
            "proj_text":   proj_text,
            "proj_color":  proj_color,
        })

    pacer_standings: dict = {d: [] for d in DISTRICT_ORDER}
    for cid, dist_counts in _comp_dist_counts.items():
        name = _comp_name_map.get(cid, "Unknown")
        is_own = (cid == own_id)
        for dist, count in dist_counts.items():
            if dist in pacer_standings:
                pacer_standings[dist].append({
                    "name": name, "count": count, "is_own": is_own,
                    "period": _dist_latest.get(dist),
                })
    for dist in DISTRICT_ORDER:
        pacer_standings[dist].sort(key=lambda r: r["count"], reverse=True)
        pacer_standings[dist] = pacer_standings[dist][:6]

    # Gap-to-#1 by market: avg review count of rank-1 pack firms vs own-firm reviews
    # Uses rating_count from LocalPackRanking.result_data (DataForSEO Maps API).
    _pack_latest = (
        db.query(cast(LocalPackRanking.scraped_at, Date))
        .filter(
            LocalPackRanking.market.in_(list(MARKET_DISPLAY.keys())),
            LocalPackRanking.in_pack == True,
        )
        .order_by(LocalPackRanking.scraped_at.desc())
        .first()
    )
    gap_to_1_by_market: dict = {}
    if _pack_latest:
        _pack_date = _pack_latest[0]
        _r1_rows = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.rank_position == 1,
                LocalPackRanking.in_pack == True,
                LocalPackRanking.market.in_(list(MARKET_DISPLAY.keys())),
                cast(LocalPackRanking.scraped_at, Date) == _pack_date,
            )
            .all()
        )
        _r1_counts: dict = defaultdict(list)
        _r1_names: dict = {}
        for r in _r1_rows:
            rd = r.result_data or {}
            rc = rd.get("rating_count")
            if rc is not None:
                _r1_counts[r.market].append(rc)
            if r.market not in _r1_names:
                _r1_names[r.market] = (
                    rd.get("title", "—") if not r.is_own_firm
                    else (own_firm.name if own_firm else "Duncan Law")
                )
        for market, counts in _r1_counts.items():
            avg_rank1 = round(sum(counts) / len(counts))
            own_rev = reviews_by_market.get(market, {}).get("review_count")
            gap = (avg_rank1 - own_rev) if own_rev is not None else None
            gap_to_1_by_market[market] = {
                "rank1_name":    _r1_names.get(market, "—"),
                "rank1_reviews": avg_rank1,
                "own_reviews":   own_rev,
                "gap":           gap,
                "is_leading":    gap is not None and gap <= 0,
            }

    return {
        "rankings_by_market":         rankings_by_market,
        "rank_direction":             rank_direction,
        "reviews_by_market":          reviews_by_market,
        "own_review_deltas":          own_review_deltas,
        "own_review_rates":           own_review_rates,
        "velocity_leaders":           velocity_leaders,
        "open_alerts":                open_alerts,
        "rankings_as_of":             latest_date,
        "base_url":                   settings.app_base_url,
        "priority_action":            priority_action,
        "district_review_standings":  district_review_standings,
        "pacer_standings":            pacer_standings,
        "market_velocity":            market_velocity,
        "pack_entries_by_market":     dict(pack_entries_by_market),
        "gap_to_1_by_market":         gap_to_1_by_market,
    }


def _generate_priority(rankings: dict, reviews: dict) -> dict:
    """Synthesise rankings and review data into a single priority recommendation."""
    # Restrict gap analysis to own-firm markets — EDNC cities are tracked for
    # competitor intelligence but Duncan Law has no offices there, so they
    # should never surface as "pack gaps" in the digest.
    own_rankings = {m: v for m, v in rankings.items() if m in MARKET_ORDER}

    # Critical: listings with fewer than 5 reviews — directly limits neutral search rankings
    thin = [
        (MARKET_DISPLAY.get(m, m), d["review_count"])
        for m, d in reviews.items()
        if d["review_count"] < 5
    ]
    if thin:
        markets = ", ".join(f"{name} ({count})" for name, count in thin)
        return {
            "level": "high",
            "headline": "Review building — immediate priority",
            "body": (
                f"{markets} {'review' if sum(c for _, c in thin) == 1 else 'reviews'} on "
                f"{'that listing' if len(thin) == 1 else 'those listings'}. "
                "Competitors in those markets have 37–60 reviews. Ask every satisfied client "
                "from those offices to leave a Google review this week."
            ),
        }

    # Pack gaps — not in 3-pack for a keyword (own-firm markets only)
    gaps = [
        (MARKET_DISPLAY.get(m, m), d["gaps"][0])
        for m, d in own_rankings.items()
        if d["gaps"]
    ]
    if gaps:
        market_name, kw = gaps[0]
        return {
            "level": "medium",
            "headline": f"Pack gap in {market_name}",
            "body": (
                f'Duncan Law is not in the 3-pack for "{kw}". '
                "Compare your GBP listing categories and review count against the two firms "
                "holding the top spots — review count is the most likely lever."
            ),
        }

    # Review count below 20 — yellow flag
    low = [
        (MARKET_DISPLAY.get(m, m), d["review_count"])
        for m, d in reviews.items()
        if d["review_count"] < 20
    ]
    if low:
        markets = ", ".join(f"{name} ({count})" for name, count in low)
        return {
            "level": "medium",
            "headline": "Build review volume in smaller markets",
            "body": (
                f"{markets}. Aim for 30+ in each market to strengthen ranking stability."
            ),
        }

    return {
        "level": "good",
        "headline": "Strong week across all markets",
        "body": "Duncan Law is in the 3-pack for all tracked keywords. Maintain review request cadence to protect your positions.",
    }


# ── HTML builder ──────────────────────────────────────────────────────────────

def _build_html(ctx: dict, week_str: str) -> str:
    base_url = ctx["base_url"].rstrip("/")
    sections = []

    # ── AI Narrative Briefing ─────────────────────────────────────────────────
    narrative = ctx.get("ai_narrative", "")
    if narrative:
        # Split on double newlines → paragraphs; fall back to single newlines
        raw_paras = [p.strip() for p in narrative.split("\n\n") if p.strip()]
        if len(raw_paras) < 2:
            raw_paras = [p.strip() for p in narrative.split("\n") if p.strip()]
        para_html = "".join(
            f'<p style="margin:0 0 14px;font-size:14px;color:#1f2937;line-height:1.75;">{p}</p>'
            for p in raw_paras
        )
        sections.append(
            f'<tr><td style="padding:24px;border-bottom:1px solid #e5e7eb;'
            f'background:linear-gradient(135deg,#f0f7ff 0%,#fafbff 100%);">'
            f'<div style="font-size:10px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.1em;color:#2563eb;margin-bottom:14px;">'
            f'Market Intelligence Briefing &nbsp;·&nbsp; Week of {week_str}'
            f'</div>'
            f'{para_html}'
            f'</td></tr>'
        )

    # ── Priority card ─────────────────────────────────────────────────────────
    pa = ctx.get("priority_action", {})
    level_colors = {
        "high":   ("#fee2e2", "#991b1b", "#dc2626"),
        "medium": ("#fef3c7", "#92400e", "#d97706"),
        "good":   ("#d1fae5", "#065f46", "#059669"),
    }
    bg, fg, accent = level_colors.get(pa.get("level", "good"), level_colors["good"])
    sections.append(f"""<tr><td style="padding:20px 24px;border-bottom:1px solid #e5e7eb;background:{bg};">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:{accent};margin-bottom:6px;">
        This Week's Priority
      </div>
      <div style="font-size:14px;font-weight:600;color:{fg};margin-bottom:6px;">{pa.get("headline", "")}</div>
      <div style="font-size:13px;color:{fg};opacity:.85;line-height:1.6;">{pa.get("body", "")}</div>
    </td></tr>""")

    # ── 4-Week Roadmap section ────────────────────────────────────────────────
    ai_roadmap = ctx.get("ai_recommendations", {})
    roadmap_weeks = ai_roadmap.get("weeks", []) if isinstance(ai_roadmap, dict) else []
    if roadmap_weeks:
        _week_accents = ["#2563eb", "#7c3aed", "#059669", "#d97706"]
        _week_bgs     = ["#dbeafe", "#ede9fe", "#d1fae5", "#fef3c7"]
        _week_fgs     = ["#1e3a8a", "#4c1d95", "#065f46", "#78350f"]

        week_blocks = ""
        for w in roadmap_weeks:
            idx   = min(w["week"] - 1, 3)
            acc   = _week_accents[idx]
            wbg   = _week_bgs[idx]
            wfg   = _week_fgs[idx]
            label = f"WEEK {w['week']} OF 4"
            theme = w["theme"]
            total_min = sum(t["minutes"] for t in w["tasks"])

            task_rows = ""
            for t in w["tasks"]:
                mkt_badge = ""
                if t["market"] and t["market"] != "All Markets":
                    mkt_badge = (
                        f'<span style="background:#f3f4f6;color:#374151;padding:1px 6px;'
                        f'border-radius:3px;font-size:10px;font-weight:600;margin-right:6px;">'
                        f'{t["market"]}</span>'
                    )
                task_rows += (
                    f'<tr><td style="padding:10px 0;border-bottom:1px solid #f3f4f6;">'
                    f'<div style="display:flex;align-items:flex-start;gap:8px;">'
                    f'<span style="color:{acc};font-size:14px;line-height:1;margin-top:1px;flex-shrink:0;">☐</span>'
                    f'<div style="flex:1;">'
                    f'<div style="font-size:13px;color:#111827;line-height:1.5;margin-bottom:3px;">'
                    f'{mkt_badge}{t["task"]}</div>'
                    f'<div style="font-size:11px;color:#6b7280;">'
                    f'{t["why"]} &nbsp;·&nbsp; ~{t["minutes"]} min'
                    f'</div>'
                    f'</div>'
                    f'</div>'
                    f'</td></tr>'
                )

            week_blocks += (
                f'<div style="margin-bottom:16px;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;">'
                f'<div style="background:{wbg};border-left:4px solid {acc};padding:8px 12px;'
                f'display:flex;align-items:center;justify-content:space-between;">'
                f'<div>'
                f'<span style="font-size:10px;font-weight:700;color:{wfg};'
                f'text-transform:uppercase;letter-spacing:.07em;">{label}</span>'
                f'<span style="font-size:13px;font-weight:600;color:{wfg};margin-left:10px;">{theme}</span>'
                f'</div>'
                f'<span style="font-size:11px;color:{wfg};opacity:.7;">~{total_min} min total</span>'
                f'</div>'
                f'<div style="padding:0 12px;">'
                f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
                f'{task_rows}'
                f'</table>'
                f'</div>'
                f'</div>'
            )

        sections.append(_section(
            "4-Week SEO Roadmap",
            f'''<p style="margin:0 0 14px;font-size:12px;color:#6b7280;">
              Sequenced by impact and dependency — reviews in early weeks produce ranking gains by week 3–4.
              Generated from this week's live data.
            </p>
            {week_blocks}
            <p style="margin-top:4px;font-size:12px;color:#6b7280;">
              <a href="{base_url}/dashboard" style="color:#3b82f6;">Open full dashboard →</a>
            </p>''',
        ))

    # ── Rankings section ──────────────────────────────────────────────────────
    rank_rows = ""
    as_of = ctx["rankings_as_of"]
    for market in MARKET_ORDER:
        label = MARKET_DISPLAY.get(market, market)
        data  = ctx["rankings_by_market"].get(market)
        if not data:
            rank_rows += _tr(label, "—", "—")
            continue
        in_pack   = data["in_pack"]
        total     = data["total"]
        gaps      = data["gaps"]
        positions = sorted(data.get("positions", []))

        # Position display e.g. "#1 #1 #3"
        pos_str = " ".join(f"#{p}" for p in positions) if positions else "—"

        if in_pack == total and total > 0:
            badge = _badge("green", f"{in_pack}/{total} in pack")
        elif in_pack > 0:
            badge = _badge("yellow", f"{in_pack}/{total} in pack")
        else:
            badge = _badge("red", "Not in pack")

        direction = ctx.get("rank_direction", {}).get(market, "same")
        arrow, arrow_color = {"up": ("↑", "#059669"), "down": ("↓", "#dc2626"), "same": ("→", "#9ca3af")}.get(direction, ("—", "#9ca3af"))
        arrow_html = f'<span style="color:{arrow_color};font-weight:700;font-size:14px;">{arrow}</span>'

        gap_text = ""
        if gaps:
            short_kw = gaps[0].replace(" Greensboro", "").replace(" Winston-Salem", "") \
                               .replace(" High Point", "").replace(" Charlotte", "") \
                               .replace(" Salisbury", "").replace(" Asheville", "")
            gap_label = f"{len(gaps)} gap{'s' if len(gaps) > 1 else ''}: {short_kw}"
            gap_text = f'<div style="font-size:11px;color:#dc2626;margin-top:2px;">{gap_label}</div>'
        rank_rows += _tr(label, pos_str, arrow_html, badge, gap_text)

    sections.append(_section(
        f'3-Pack Positions' + (f' — as of {as_of.strftime("%b %d")}' if as_of else ''),
        f'''<table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Market</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Positions</th>
            <th style="text-align:center;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Week</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Status</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Note</th>
          </tr>
          {rank_rows}
        </table>
        <p style="margin-top:12px;font-size:12px;color:#6b7280;">
          <a href="{base_url}/rankings" style="color:#3b82f6;">View full rankings →</a>
        </p>''',
    ))

    # ── Pack Changes section ──────────────────────────────────────────────────
    pack_entries = ctx.get("pack_entries_by_market", {})
    if pack_entries:
        total_entries = sum(len(v) for v in pack_entries.values())
        entry_rows = ""
        for market in MARKET_ORDER:
            entries = pack_entries.get(market, [])
            if not entries:
                continue
            market_label = MARKET_DISPLAY.get(market, market.replace("_", " ").title())
            for e in entries:
                pos_str = f"#{e['position']}" if e["position"] else "—"
                entry_rows += (
                    f'<tr>'
                    f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;font-weight:600;color:#374151;">'
                    f'{market_label}</td>'
                    f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;color:#374151;">'
                    f'{e["competitor"][:42]}</td>'
                    f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;color:#6b7280;">'
                    f'{e["keyword"]}</td>'
                    f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;text-align:center;">'
                    f'<span style="background:#fff7ed;color:#c2410c;padding:2px 8px;border-radius:4px;'
                    f'font-size:11px;font-weight:700;">{pos_str}</span></td>'
                    f'</tr>'
                )
        sections.append(_section(
            f"3-Pack Changes This Week — {total_entries} new entr{'y' if total_entries == 1 else 'ies'} detected",
            f'''<p style="margin:0 0 10px;font-size:13px;color:#6b7280;">
              Competitors that newly appeared in your 3-pack since last Monday.
            </p>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Market</th>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Competitor</th>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Keyword</th>
                <th style="text-align:center;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Position</th>
              </tr>
              {entry_rows}
            </table>
            <p style="margin-top:12px;font-size:12px;color:#6b7280;">
              <a href="{base_url}/rankings" style="color:#3b82f6;">View full rankings →</a>
            </p>''',
        ))

    # ── Reviews section ───────────────────────────────────────────────────────
    review_rows = ""
    own_deltas   = ctx.get("own_review_deltas", {})
    g1_by_market = ctx.get("gap_to_1_by_market", {})
    for market in MARKET_ORDER:
        label    = MARKET_DISPLAY.get(market, market)
        data     = ctx["reviews_by_market"].get(market)
        delta    = own_deltas.get(market)
        gap_info = g1_by_market.get(market)
        if not data:
            review_rows += _tr(label, "—", "—", "—", "—", "")
            continue
        count  = data["review_count"]
        rating = data["rating"]
        stars  = f"{rating:.1f} ★" if rating else "—"
        if count < 5:
            badge = _badge("red", f"{count} reviews")
            note  = "Priority: review building needed"
        elif count < 20:
            badge = _badge("yellow", f"{count} reviews")
            note  = "Build toward 30+"
        else:
            badge = _badge("green", f"{count} reviews")
            note  = ""
        if delta is not None:
            if delta > 0:
                delta_cell = f'<span style="color:#059669;font-weight:700;">+{delta}</span>'
            elif delta < 0:
                delta_cell = f'<span style="color:#dc2626;font-weight:700;">{delta}</span>'
            else:
                delta_cell = '<span style="color:#9ca3af;">+0</span>'
        else:
            delta_cell = '<span style="color:#9ca3af;">—</span>'
        if gap_info and gap_info["is_leading"]:
            gap_cell = '<span style="color:#059669;font-weight:600;">Leading ✓</span>'
        elif gap_info and gap_info["gap"] is not None and gap_info["gap"] > 0:
            rival = gap_info["rank1_name"]
            rival_short = (rival[:22] + "…") if len(rival) > 22 else rival
            gap_cell = (
                f'<span style="color:#ea580c;font-weight:700;">+{gap_info["gap"]}</span>'
                f'<br><span style="font-size:10px;color:#9ca3af;">vs {rival_short}'
                f' ({gap_info["rank1_reviews"]})</span>'
            )
        else:
            gap_cell = '<span style="color:#9ca3af;">—</span>'
        review_rows += _tr(label, stars, badge, gap_cell, delta_cell, note)

    sections.append(_section(
        "Duncan Law — Google Reviews by Market",
        f'''<table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Market</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Rating</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Reviews</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Gap to #1</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Δ Week</th>
            <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Action</th>
          </tr>
          {review_rows}
        </table>
        <p style="margin-top:12px;font-size:12px;color:#6b7280;">
          <a href="{base_url}/reviews" style="color:#3b82f6;">View full review data →</a>
        </p>''',
    ))

    # ── Review Velocity section ───────────────────────────────────────────────
    vel = ctx.get("market_velocity", [])
    if vel:
        vel_rows = ""
        color_map = {"green": "#059669", "red": "#dc2626", "blue": "#2563eb", "gray": "#6b7280"}
        bg_map    = {"green": "#d1fae5", "red": "#fee2e2", "blue": "#dbeafe", "gray": "#f3f4f6"}
        for v in vel:
            od = v["own_delta"]
            rd = v["rival_delta"]
            _fmt = lambda v: f"+{round(v, 1)}" if v > 0 else (str(round(v, 1)) if v != 0 else "±0")
            own_d_str   = _fmt(od)
            rival_d_str = _fmt(rd)
            pc = v["proj_color"]
            vel_rows += (
                f'<tr>'
                f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;font-weight:600;">{v["display"]}</td>'
                f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;">'
                f'  {v["own_count"]:,} <span style="color:#9ca3af;font-size:11px;">({own_d_str}/wk avg)</span></td>'
                f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;color:#374151;">'
                f'  {v["rival_name"]}<br>'
                f'  <span style="font-size:11px;color:#9ca3af;">{v["rival_count"]:,} ({rival_d_str}/wk avg)</span></td>'
                f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;">'
                f'  <span style="background:{bg_map.get(pc,"#f3f4f6")};color:{color_map.get(pc,"#374151")};'
                f'padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;">{v["proj_text"]}</span>'
                f'</td>'
                f'</tr>'
            )
        sections.append(_section(
            "Review Velocity — Duncan Law vs. Market Leader",
            f'''<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Market</th>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Duncan Law</th>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Top Rival</th>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Projection</th>
              </tr>
              {vel_rows}
            </table>
            <p style="margin-top:12px;font-size:12px;color:#6b7280;">
              <a href="{base_url}/reviews" style="color:#3b82f6;">View full velocity chart →</a>
            </p>''',
        ))

    # ── District Review Intelligence section ─────────────────────────────────
    dist_standings = ctx.get("district_review_standings", {})
    dist_blocks = ""
    for dist in DISTRICT_ORDER:
        rows = dist_standings.get(dist, [])
        if not rows:
            continue
        bg, fg, accent = DISTRICT_COLORS[dist]
        label = DISTRICT_LABELS[dist]
        row_html = ""
        for i, row in enumerate(rows[:6]):
            name = row["name"][:42] + ("…" if len(row["name"]) > 42 else "")
            own_badge = (
                ' <span style="background:#dbeafe;color:#1e40af;padding:1px 5px;'
                'border-radius:3px;font-size:10px;font-weight:700;">You</span>'
            ) if row["is_own"] else ""
            bold = "font-weight:600;" if row["is_own"] else ""
            num_color = "#059669" if i == 0 else "#374151"
            row_html += (
                f'<tr><td style="padding:5px 8px;font-size:13px;border-bottom:1px solid #f3f4f6;{bold}color:#374151;">'
                f'{name}{own_badge}</td>'
                f'<td style="padding:5px 8px;font-size:13px;border-bottom:1px solid #f3f4f6;'
                f'text-align:right;font-weight:600;color:{num_color};">{row["count"]:,}</td></tr>'
            )
        dist_blocks += (
            f'<div style="margin-bottom:14px;">'
            f'<div style="background:{bg};border-left:3px solid {accent};padding:5px 10px;'
            f'margin-bottom:6px;border-radius:0 4px 4px 0;">'
            f'<span style="font-size:11px;font-weight:700;color:{fg};text-transform:uppercase;'
            f'letter-spacing:.06em;">{dist}</span>'
            f'<span style="font-size:11px;color:{fg};opacity:.75;margin-left:8px;">{label}</span>'
            f'</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            f'<tr><th style="text-align:left;font-size:10px;color:#6b7280;border-bottom:1px solid #e5e7eb;'
            f'padding:4px 8px;text-transform:uppercase;letter-spacing:.05em;">Firm</th>'
            f'<th style="text-align:right;font-size:10px;color:#6b7280;border-bottom:1px solid #e5e7eb;'
            f'padding:4px 8px;text-transform:uppercase;letter-spacing:.05em;">Google Reviews</th></tr>'
            f'{row_html}</table></div>'
        )
    if dist_blocks:
        sections.append(_section(
            "District Review Intelligence",
            f'{dist_blocks}<p style="margin:4px 0 0;font-size:12px;color:#6b7280;">'
            f'<a href="{base_url}/reviews" style="color:#3b82f6;">View full review data →</a></p>',
        ))

    # ── Competitor activity section ───────────────────────────────────────────
    leaders = ctx.get("velocity_leaders", [])
    if leaders:
        vel_rows = ""
        for v in leaders:
            name = v["name"][:38] + ("…" if len(v["name"]) > 38 else "")
            vel_rows += f'''<tr>
              <td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;">{name}</td>
              <td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;color:#6b7280;">{v["total"]:,} total</td>
              <td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;">
                <span style="background:#fff7ed;color:#c2410c;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;">+{v["delta"]} this week</span>
              </td>
            </tr>'''
        sections.append(_section(
            "Competitor Review Activity",
            f'''<p style="margin:0 0 10px;font-size:13px;color:#6b7280;">Top gainers since last collection — may indicate a review campaign running</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Firm</th>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Total Reviews</th>
                <th style="text-align:left;font-size:11px;color:#6b7280;border-bottom:1px solid #e5e7eb;padding:6px 8px;">Gained</th>
              </tr>
              {vel_rows}
            </table>
            <p style="margin-top:12px;font-size:12px;color:#6b7280;">
              <a href="{base_url}/reviews" style="color:#3b82f6;">View full review data →</a>
            </p>''',
        ))

    # ── PACER District Standings section ─────────────────────────────────────
    pacer = ctx.get("pacer_standings", {})
    pacer_blocks = ""
    for dist in DISTRICT_ORDER:
        rows = pacer.get(dist, [])
        if not rows:
            continue
        bg, fg, accent = DISTRICT_COLORS[dist]
        label = DISTRICT_LABELS[dist]
        period_str = ""
        if rows and rows[0].get("period"):
            try:
                period_str = f' — {rows[0]["period"].strftime("%b %Y")}'
            except Exception:
                pass
        row_html = ""
        for row in rows:
            name = row["name"][:38] + ("…" if len(row["name"]) > 38 else "")
            own_badge = (
                ' <span style="background:#dbeafe;color:#1e40af;padding:1px 5px;'
                'border-radius:3px;font-size:10px;font-weight:700;">You</span>'
            ) if row["is_own"] else ""
            bold = "font-weight:600;" if row["is_own"] else ""
            row_html += (
                f'<tr><td style="padding:5px 8px;font-size:13px;border-bottom:1px solid #f3f4f6;{bold}color:#374151;">'
                f'{name}{own_badge}</td>'
                f'<td style="padding:5px 8px;font-size:13px;border-bottom:1px solid #f3f4f6;'
                f'text-align:right;font-weight:600;color:#374151;">{row["count"]}</td></tr>'
            )
        pacer_blocks += (
            f'<div style="margin-bottom:14px;">'
            f'<div style="background:{bg};border-left:3px solid {accent};padding:5px 10px;'
            f'margin-bottom:6px;border-radius:0 4px 4px 0;">'
            f'<span style="font-size:11px;font-weight:700;color:{fg};text-transform:uppercase;'
            f'letter-spacing:.06em;">{dist}</span>'
            f'<span style="font-size:11px;color:{fg};opacity:.75;margin-left:8px;">{label}{period_str}</span>'
            f'</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            f'<tr><th style="text-align:left;font-size:10px;color:#6b7280;border-bottom:1px solid #e5e7eb;'
            f'padding:4px 8px;text-transform:uppercase;letter-spacing:.05em;">Firm</th>'
            f'<th style="text-align:right;font-size:10px;color:#6b7280;border-bottom:1px solid #e5e7eb;'
            f'padding:4px 8px;text-transform:uppercase;letter-spacing:.05em;">Cases (month)</th></tr>'
            f'{row_html}</table></div>'
        )
    if pacer_blocks:
        sections.append(_section(
            "PACER Filing Standings",
            f'{pacer_blocks}<p style="margin:4px 0 0;font-size:12px;color:#6b7280;">'
            f'<a href="{base_url}/filings" style="color:#3b82f6;">View full filing data →</a></p>',
        ))

    # ── Alerts section ────────────────────────────────────────────────────────
    if ctx["open_alerts"]:
        alert_items = ""
        type_labels = {
            "pack_drop":             "Pack drop",
            "competitor_pack_entry": "Competitor entered pack",
            "review_gap":            "Review gap",
            "pacer_volume_spike":    "PACER volume spike",
            "pack_convergence":      "Competitor closing on pack position",
        }
        for a in ctx["open_alerts"]:
            label = type_labels.get(a.alert_type, a.alert_type)
            msg   = a.detail.get("message", "") if a.detail else ""
            sev_color = "#dc2626" if a.severity == "immediate" else "#d97706"
            alert_items += f'''
              <div style="border-left:3px solid {sev_color};background:#fafafa;padding:10px 12px;margin-bottom:8px;border-radius:0 4px 4px 0;">
                <div style="font-size:12px;font-weight:600;color:{sev_color};">{label}</div>
                <div style="font-size:12px;color:#374151;margin-top:2px;">{msg[:140]}</div>
              </div>'''
        sections.append(_section(
            f"{len(ctx['open_alerts'])} Open Alert{'s' if len(ctx['open_alerts']) != 1 else ''}",
            f'''{alert_items}
            <p style="margin-top:12px;font-size:12px;color:#6b7280;">
              <a href="{base_url}/alerts" style="color:#3b82f6;">View all alerts →</a>
            </p>''',
        ))
    else:
        sections.append(_section(
            "Alerts",
            '<p style="font-size:13px;color:#059669;">✓ No open alerts — all clear</p>',
        ))

    # ── One Action block ─────────────────────────────────────────────────────
    pa = ctx.get("priority_action", {})
    if pa.get("level") in ("high", "medium"):
        action_bg    = "#7f1d1d" if pa["level"] == "high" else "#1e3a8a"
        action_label = "#fca5a5" if pa["level"] == "high" else "#93c5fd"
        action_btn   = "#ef4444" if pa["level"] == "high" else "#3b82f6"
        sections.append(
            f'<tr><td style="padding:24px;background:{action_bg};">'
            f'<div style="text-align:center;">'
            f'<div style="font-size:11px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.08em;color:{action_label};margin-bottom:10px;">This Week\'s One Action</div>'
            f'<div style="font-size:16px;font-weight:700;color:#ffffff;margin-bottom:6px;">'
            f'{pa.get("headline", "")}</div>'
            f'<div style="font-size:13px;color:#e2e8f0;opacity:.85;margin-bottom:16px;line-height:1.5;">'
            f'{pa.get("body", "")[:160]}</div>'
            f'<a href="{base_url}/dashboard" style="background:{action_btn};color:#ffffff;'
            f'padding:10px 28px;border-radius:6px;text-decoration:none;font-size:13px;'
            f'font-weight:600;display:inline-block;">Open Dashboard →</a>'
            f'</div></td></tr>'
        )

    body = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Duncan Law SEO Intelligence</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0;">
    <tr><td>
      <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">
        <!-- Header -->
        <tr><td style="background:#0f1117;padding:24px;">
          <div style="font-size:18px;font-weight:700;color:#e2e8f0;">Duncan Law SEO Intelligence</div>
          <div style="font-size:13px;color:#8892a4;margin-top:4px;">Week of {week_str}</div>
        </td></tr>
        <!-- Body -->
        {body}
        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:16px 24px;border-top:1px solid #e5e7eb;">
          <p style="margin:0;font-size:12px;color:#9ca3af;">
            <a href="{base_url}/dashboard" style="color:#3b82f6;">Open full dashboard</a>
            &nbsp;·&nbsp; Sent every Monday at 7 AM ET
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rolling_weekly_rate(snaps: list, n_weeks: int = 4) -> float:
    """Average weekly review gain over the last n_weeks of history (snaps sorted newest-first)."""
    valid = [s for s in snaps if s.review_count is not None]
    if len(valid) < 2:
        return 0.0
    window = valid[:n_weeks + 1]
    newest, oldest = window[0], window[-1]
    if newest.review_count <= oldest.review_count:
        return 0.0
    elapsed = (newest.snapped_at - oldest.snapped_at).total_seconds() / 86400
    return (newest.review_count - oldest.review_count) / (elapsed / 7) if elapsed >= 1 else 0.0


def _section(title: str, content: str) -> str:
    return f"""<tr><td style="padding:20px 24px;border-bottom:1px solid #e5e7eb;">
      <div style="font-size:14px;font-weight:600;color:#111827;margin-bottom:12px;">{title}</div>
      {content}
    </td></tr>"""


def _badge(color: str, text: str) -> str:
    colors = {
        "green":  ("background:#d1fae5", "color:#065f46"),
        "yellow": ("background:#fef3c7", "color:#92400e"),
        "red":    ("background:#fee2e2", "color:#991b1b"),
    }
    bg, fg = colors.get(color, ("background:#f3f4f6", "color:#374151"))
    return (
        f'<span style="{bg};{fg};padding:2px 8px;border-radius:4px;'
        f'font-size:11px;font-weight:600;">{text}</span>'
    )


def _tr(*cells) -> str:
    tds = "".join(
        f'<td style="padding:8px;font-size:13px;border-bottom:1px solid #f3f4f6;vertical-align:top;">{c}</td>'
        for c in cells if c != ""
    )
    return f"<tr>{tds}</tr>"
