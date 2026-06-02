import logging
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import cast, Date, func
from app.models.alerts import Alert
from app.models.rankings import LocalPackRanking
from app.models.competitor import Competitor
from app.models.base import new_uuid

logger = logging.getLogger(__name__)


def check_pack_alerts(db: Session, own_firm_id: str) -> None:
    """
    After daily rankings are stored, compare today vs yesterday for own firm.
    Fires immediate alerts for:
      - pack_drop: own firm was in pack yesterday, not today
      - competitor_pack_entry: competitor newly in pack with own firm today
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    own_today = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm_id,
            cast(LocalPackRanking.scraped_at, Date) == today,
        )
        .all()
    )

    for row in own_today:
        _check_pack_drop(db, row, own_firm_id, yesterday)
        _check_competitor_entry(db, row, own_firm_id, yesterday)

    db.commit()


def _check_pack_drop(db: Session, today_row: LocalPackRanking, own_firm_id: str, yesterday: date) -> None:
    yesterday_row = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm_id,
            LocalPackRanking.keyword == today_row.keyword,
            LocalPackRanking.city == today_row.city,
            cast(LocalPackRanking.scraped_at, Date) == yesterday,
        )
        .first()
    )

    was_in_pack = yesterday_row and yesterday_row.in_pack
    is_in_pack = today_row.in_pack

    if was_in_pack and not is_in_pack:
        existing = (
            db.query(Alert)
            .filter(
                Alert.alert_type == "pack_drop",
                Alert.keyword == today_row.keyword,
                Alert.market == today_row.market,
                cast(Alert.triggered_at, Date) == date.today(),
            )
            .first()
        )
        if not existing:
            market_display = (today_row.market or today_row.city or "this market").replace("_", " ").title()
            alert = Alert(
                id=new_uuid(),
                alert_type="pack_drop",
                severity="immediate",
                competitor_id=own_firm_id,
                keyword=today_row.keyword,
                market=today_row.market,
                detail={
                    "keyword": today_row.keyword,
                    "city": today_row.city,
                    "previous_position": yesterday_row.rank_position,
                    "current_position": None,
                    "message": f"Duncan Law dropped out of the 3-pack for '{today_row.keyword}'",
                    "recommendation": (
                        f"1) Search '{today_row.keyword}' in an incognito browser right now and note which 3 firms "
                        f"are holding the pack — compare their review counts to yours in {market_display}. "
                        f"2) Send review requests to every {market_display} client from the past 30 days today — "
                        f"review count is the most common cause of a pack drop. "
                        f"3) Post a fresh Google Business Profile update for your {market_display} location "
                        f"(a tip, office photo, or recent case result) to signal activity to Google."
                    ),
                },
                triggered_at=datetime.now(timezone.utc),
            )
            db.add(alert)
            logger.warning(f"ALERT: Pack drop — {today_row.keyword} in {today_row.city}")
            _send_immediate_alert_email(alert, db)


def _check_competitor_entry(db: Session, today_row: LocalPackRanking, own_firm_id: str, yesterday: date) -> None:
    if not today_row.in_pack:
        return

    # Find competitor rankings for same keyword today
    comp_today = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.keyword == today_row.keyword,
            LocalPackRanking.city == today_row.city,
            LocalPackRanking.is_own_firm == False,
            LocalPackRanking.in_pack == True,
            cast(LocalPackRanking.scraped_at, Date) == date.today(),
        )
        .all()
    )

    for comp_row in comp_today:
        # Was this competitor in the pack yesterday?
        comp_yesterday = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.competitor_id == comp_row.competitor_id,
                LocalPackRanking.keyword == today_row.keyword,
                LocalPackRanking.city == today_row.city,
                LocalPackRanking.in_pack == True,
                cast(LocalPackRanking.scraped_at, Date) == yesterday,
            )
            .first()
        )

        if comp_yesterday:
            continue

        # Don't fire on first-run initialization — require at least one prior
        # data point for this competitor/keyword before treating it as new entry
        has_prior_data = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.competitor_id == comp_row.competitor_id,
                LocalPackRanking.keyword == today_row.keyword,
                LocalPackRanking.city == today_row.city,
                cast(LocalPackRanking.scraped_at, Date) < date.today(),
            )
            .first()
        )
        if not has_prior_data:
            continue

        comp = db.query(Competitor).filter(Competitor.id == comp_row.competitor_id).first()
        comp_name = comp.name if comp else "Unknown competitor"

        existing = (
            db.query(Alert)
            .filter(
                Alert.alert_type == "competitor_pack_entry",
                Alert.competitor_id == comp_row.competitor_id,
                Alert.keyword == today_row.keyword,
                Alert.market == today_row.market,
                cast(Alert.triggered_at, Date) == date.today(),
            )
            .first()
        )
        if not existing:
            market_display = (today_row.market or today_row.city or "this market").replace("_", " ").title()
            pos_str = f"#{comp_row.rank_position}" if comp_row.rank_position else "an unknown position"
            alert = Alert(
                id=new_uuid(),
                alert_type="competitor_pack_entry",
                severity="weekly_digest",
                competitor_id=comp_row.competitor_id,
                keyword=today_row.keyword,
                market=today_row.market,
                detail={
                    "keyword": today_row.keyword,
                    "city": today_row.city,
                    "competitor_name": comp_name,
                    "position": comp_row.rank_position,
                    "message": f"{comp_name} newly entered the 3-pack for '{today_row.keyword}'",
                    "recommendation": (
                        f"{comp_name} entered the pack at {pos_str} for '{today_row.keyword}' in {market_display}. "
                        f"Look up their Google Business Profile and compare their review count to yours — "
                        f"if they recently surpassed you in reviews, that's likely what moved them in. "
                        f"Prioritize review requests from {market_display} clients this week to protect your position."
                    ),
                },
                triggered_at=datetime.now(timezone.utc),
            )
            db.add(alert)
            logger.warning(f"ALERT: Competitor entered pack — {comp_name} for '{today_row.keyword}' in {today_row.city}")


def _send_immediate_alert_email(alert: Alert, db: Session) -> None:
    """Send an immediate alert email via Resend. Phase 5 wires this fully."""
    from app.config import settings
    if not settings.resend_api_key:
        logger.info("Resend not configured — skipping immediate alert email")
        return

    try:
        import resend
        resend.api_key = settings.resend_api_key

        rec = alert.detail.get("recommendation", "")
        rec_block = (
            f"<div style='margin-top:16px;background:#eff6ff;border-left:3px solid #2563eb;"
            f"padding:12px 14px;border-radius:0 6px 6px 0;'>"
            f"<div style='font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;"
            f"color:#2563eb;margin-bottom:6px;'>What to do</div>"
            f"<div style='font-size:13px;color:#1e3a8a;line-height:1.6;'>{rec}</div>"
            f"</div>"
        ) if rec else ""

        if alert.alert_type == "pack_drop":
            subject = f"ALERT: Duncan Law dropped from 3-pack — {alert.detail.get('keyword')}"
            body = (
                f"<p><strong>Duncan Law has dropped out of the Google 3-pack.</strong></p>"
                f"<p><strong>Keyword:</strong> {alert.detail.get('keyword')}<br>"
                f"<strong>Market:</strong> {alert.market}<br>"
                f"<strong>Previous position:</strong> #{alert.detail.get('previous_position')}<br>"
                f"<strong>Current position:</strong> Not in pack</p>"
                f"{rec_block}"
                f"<p style='margin-top:16px;'><a href='{settings.app_base_url}/alerts'>View all alerts</a></p>"
            )
        else:
            subject = f"ALERT: Competitor entered 3-pack — {alert.detail.get('competitor_name')}"
            body = (
                f"<p><strong>{alert.detail.get('competitor_name')} has entered the Google 3-pack.</strong></p>"
                f"<p><strong>Keyword:</strong> {alert.detail.get('keyword')}<br>"
                f"<strong>Market:</strong> {alert.market}<br>"
                f"<strong>Position:</strong> #{alert.detail.get('position')}</p>"
                f"{rec_block}"
                f"<p style='margin-top:16px;'><a href='{settings.app_base_url}/alerts'>View all alerts</a></p>"
            )

        resend.Emails.send({
            "from": settings.resend_from_address,
            "to": settings.digest_recipient,
            "subject": subject,
            "html": body,
        })

        alert.emailed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(f"Immediate alert email sent: {subject}")

    except Exception as e:
        logger.error(f"Failed to send immediate alert email: {e}")


def check_convergence_alerts(db: Session, own_firm_id: str) -> None:
    """
    Daily: detect competitors consistently closing on Duncan Law's rank position.
    Fires 'pack_convergence' (digest severity) when a rival improves 2+ positions
    over 30 days AND is now within 2 spots of Duncan Law or inside the pack.
    Deduplicates to one alert per competitor/keyword/market per 28-day window.
    """
    today = date.today()
    lookback = today - timedelta(days=30)
    one_month_ago = today - timedelta(days=28)

    own_firm = db.query(Competitor).filter(Competitor.id == own_firm_id).first()
    if not own_firm:
        return

    own_recent = (
        db.query(LocalPackRanking)
        .filter(
            LocalPackRanking.competitor_id == own_firm_id,
            LocalPackRanking.in_pack == True,
            cast(LocalPackRanking.scraped_at, Date) >= lookback,
        )
        .all()
    )
    kw_market_set = set((r.keyword, r.market) for r in own_recent)

    comp_name_cache: dict = {}

    for keyword, market in kw_market_set:
        own_latest = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.competitor_id == own_firm_id,
                LocalPackRanking.keyword == keyword,
                LocalPackRanking.market == market,
                LocalPackRanking.in_pack == True,
                cast(LocalPackRanking.scraped_at, Date) >= lookback,
            )
            .order_by(LocalPackRanking.scraped_at.desc())
            .first()
        )
        if not own_latest or own_latest.rank_position is None:
            continue
        own_rank = own_latest.rank_position

        comp_rows = (
            db.query(LocalPackRanking)
            .filter(
                LocalPackRanking.keyword == keyword,
                LocalPackRanking.market == market,
                LocalPackRanking.is_own_firm == False,
                cast(LocalPackRanking.scraped_at, Date) >= lookback,
            )
            .order_by(LocalPackRanking.competitor_id, LocalPackRanking.scraped_at)
            .all()
        )

        comp_history: dict = defaultdict(list)
        for r in comp_rows:
            comp_history[r.competitor_id].append(r)

        for comp_id, hist in comp_history.items():
            if len(hist) < 3:
                continue
            ranks = [r.rank_position for r in hist if r.rank_position is not None]
            if len(ranks) < 3:
                continue

            first_rank = ranks[0]
            last_rank = ranks[-1]
            improvement = first_rank - last_rank

            if improvement < 2:
                continue
            if last_rank > own_rank + 2 and last_rank > 3:
                continue

            existing = (
                db.query(Alert)
                .filter(
                    Alert.alert_type == "pack_convergence",
                    Alert.competitor_id == comp_id,
                    Alert.keyword == keyword,
                    Alert.market == market,
                    cast(Alert.triggered_at, Date) >= one_month_ago,
                )
                .first()
            )
            if existing:
                continue

            if comp_id not in comp_name_cache:
                comp = db.query(Competitor).filter(Competitor.id == comp_id).first()
                comp_name_cache[comp_id] = comp.name if comp else "Unknown"
            comp_name = comp_name_cache[comp_id]
            market_display = market.replace("_", " ").title()

            alert = Alert(
                id=new_uuid(),
                alert_type="pack_convergence",
                severity="weekly_digest",
                competitor_id=comp_id,
                keyword=keyword,
                market=market,
                detail={
                    "keyword": keyword,
                    "market": market,
                    "competitor_name": comp_name,
                    "competitor_rank_first": first_rank,
                    "competitor_rank_latest": last_rank,
                    "own_rank": own_rank,
                    "improvement": improvement,
                    "message": (
                        f"{comp_name} improved from #{first_rank} to #{last_rank} "
                        f"for '{keyword}' in {market_display} over 30 days "
                        f"(Duncan Law at #{own_rank})."
                    ),
                    "recommendation": (
                        f"{comp_name} has climbed {improvement} positions in 30 days and is now at #{last_rank}"
                        + (f", just {last_rank - own_rank} spot{'s' if last_rank - own_rank != 1 else ''} behind you" if last_rank > own_rank else ", already ahead of you")
                        + f" for '{keyword}' in {market_display}. "
                        f"Don't wait for them to push you further down — send review requests to {market_display} clients this week "
                        f"and post a fresh update to your Google Business Profile to reinforce your position."
                    ),
                },
                triggered_at=datetime.now(timezone.utc),
            )
            db.add(alert)
            logger.info(
                f"Convergence alert: {comp_name} #{first_rank}→#{last_rank} "
                f"for '{keyword}' in {market}, Duncan Law #{own_rank}"
            )

    db.commit()


def check_review_gaps(db: Session) -> None:
    """
    Weekly: for each Duncan Law market, check if any competitor operating in
    that market has 2x+ more reviews. Fires a digest-level alert once per month
    per market so it surfaces in the weekly digest without spamming.
    """
    from app.models.competitor import Competitor, CompetitorLocation
    from app.models.reviews import ReviewSnapshot
    from datetime import date, timedelta
    from sqlalchemy import cast, Date

    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    if not own_firm:
        return

    # Latest own-firm review count per market
    own_snaps = (
        db.query(ReviewSnapshot)
        .filter(
            ReviewSnapshot.competitor_id == own_firm.id,
            ReviewSnapshot.source == "google",
            ReviewSnapshot.market != None,
        )
        .order_by(ReviewSnapshot.snapped_at.desc())
        .all()
    )
    own_by_market: dict = {}
    for s in own_snaps:
        if s.market not in own_by_market:
            own_by_market[s.market] = s.review_count or 0

    # Latest competitor review count per (competitor_id, market).
    # Snapshots are stored with market=CompetitorLocation.market (not None),
    # so key by (competitor_id, market) and fall back to (competitor_id, None)
    # for any legacy rows that pre-date per-location storage.
    comp_snaps = (
        db.query(ReviewSnapshot)
        .filter(
            ReviewSnapshot.competitor_id != own_firm.id,
            ReviewSnapshot.source == "google",
        )
        .order_by(ReviewSnapshot.snapped_at.desc())
        .all()
    )
    comp_count_by_comp_market: dict = {}
    for s in comp_snaps:
        key = (s.competitor_id, s.market)
        if key not in comp_count_by_comp_market:
            comp_count_by_comp_market[key] = s.review_count or 0

    # Check each market
    one_month_ago = date.today() - timedelta(days=28)

    for market, own_count in own_by_market.items():
        if own_count == 0:
            continue

        # Find competitors active in this market
        locs = (
            db.query(CompetitorLocation)
            .filter(CompetitorLocation.market == market)
            .all()
        )
        for loc in locs:
            comp_count = comp_count_by_comp_market.get(
                (loc.competitor_id, market),
                comp_count_by_comp_market.get((loc.competitor_id, None), 0),
            )
            if comp_count < own_count * 2:
                continue

            comp = db.query(Competitor).filter(Competitor.id == loc.competitor_id).first()
            if not comp:
                continue

            # Only fire once per market per competitor per month
            existing = (
                db.query(Alert)
                .filter(
                    Alert.alert_type == "review_gap",
                    Alert.competitor_id == loc.competitor_id,
                    Alert.market == market,
                    cast(Alert.triggered_at, Date) >= one_month_ago,
                )
                .first()
            )
            if existing:
                continue

            ratio = round(comp_count / own_count, 1)
            market_display = market.replace("_", " ").title()
            gap = comp_count - own_count
            alert = Alert(
                id=new_uuid(),
                alert_type="review_gap",
                severity="weekly_digest",
                competitor_id=loc.competitor_id,
                market=market,
                detail={
                    "market": market,
                    "competitor_name": comp.name,
                    "competitor_reviews": comp_count,
                    "duncan_law_reviews": own_count,
                    "ratio": ratio,
                    "message": (
                        f"{comp.name} has {comp_count} reviews in {market_display} "
                        f"vs. Duncan Law's {own_count} ({ratio}×). "
                        f"Prioritize review building for this market."
                    ),
                    "recommendation": (
                        f"You need {gap} more reviews to match {comp.name} in {market_display}. "
                        f"A gap this size directly threatens your 3-pack stability here. "
                        f"Set a goal of 3–5 new Google reviews from {market_display} clients per week — "
                        f"reviews collected now will improve your ranking visibility in 3–4 weeks. "
                        f"Add a review-request step to your post-filing client workflow for this office."
                    ),
                },
                triggered_at=datetime.now(timezone.utc),
            )
            db.add(alert)
            logger.info(
                f"Review gap alert: {comp.name} has {comp_count} reviews vs "
                f"Duncan Law's {own_count} in {market}"
            )

    db.commit()


def check_pacer_trends(db: Session) -> None:
    """
    Compare each competitor's last 90 days of filings to the prior 90-day
    window. Fire a digest alert when total filings increase 20%+ sustained.
    Deduplicates to one alert per competitor per 28-day window.
    """
    from app.models.competitor import Competitor
    from app.models.filings import FilingSnapshot
    from datetime import date, timedelta

    today = date.today()
    # Window boundaries: [prior_start, mid] and [mid, today]
    mid = today - timedelta(days=90)
    prior_start = mid - timedelta(days=90)

    competitors = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).all()

    for comp in competitors:
        for district in ("MDNC", "WDNC"):
            def total_for_window(start, end):
                rows = (
                    db.query(FilingSnapshot)
                    .filter(
                        FilingSnapshot.competitor_id == comp.id,
                        FilingSnapshot.district == district,
                        FilingSnapshot.period_start >= start,
                        FilingSnapshot.period_start < end,
                    )
                    .all()
                )
                return sum(r.case_count for r in rows)

            recent = total_for_window(mid, today)
            prior = total_for_window(prior_start, mid)

            if prior == 0 or recent < 5:
                continue

            pct_change = (recent - prior) / prior * 100
            if pct_change < 20:
                continue

            one_month_ago = date.today() - timedelta(days=28)
            existing = (
                db.query(Alert)
                .filter(
                    Alert.alert_type == "pacer_volume_spike",
                    Alert.competitor_id == comp.id,
                    Alert.market == district,
                    cast(Alert.triggered_at, Date) >= one_month_ago,
                )
                .first()
            )
            if existing:
                continue

            district_markets = {
                "MDNC": "Greensboro, Winston-Salem, High Point, and Salisbury",
                "WDNC": "Charlotte and Asheville",
            }
            affected_markets = district_markets.get(district, district)
            alert = Alert(
                id=new_uuid(),
                alert_type="pacer_volume_spike",
                severity="weekly_digest",
                competitor_id=comp.id,
                market=district,
                detail={
                    "competitor_name": comp.name,
                    "district": district,
                    "recent_90_day_total": recent,
                    "prior_90_day_total": prior,
                    "pct_change": round(pct_change),
                    "message": (
                        f"{comp.name} filed {recent} cases in {district} over the last 90 days "
                        f"vs. {prior} in the prior 90 days (+{round(pct_change)}%). "
                        f"They may be growing market share in this district."
                    ),
                    "recommendation": (
                        f"{comp.name} is filing {round(pct_change)}% more cases than 90 days ago — "
                        f"this typically signals aggressive client acquisition in {district}. "
                        f"Protect your share in {affected_markets} by ensuring your Google Business Profile listings "
                        f"in those markets are up to date, have recent reviews, and are posting regularly. "
                        f"A competitor filing more cases won't automatically hurt your SEO, but it does mean "
                        f"they're acquiring clients who might otherwise have found you first."
                    ),
                },
                triggered_at=datetime.now(timezone.utc),
            )
            db.add(alert)
            logger.warning(
                f"PACER spike alert: {comp.name} {district} "
                f"+{round(pct_change)}% ({prior} → {recent})"
            )

    db.commit()
