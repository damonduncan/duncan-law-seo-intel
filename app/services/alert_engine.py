import logging
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
            alert = Alert(
                id=new_uuid(),
                alert_type="competitor_pack_entry",
                severity="immediate",
                competitor_id=comp_row.competitor_id,
                keyword=today_row.keyword,
                market=today_row.market,
                detail={
                    "keyword": today_row.keyword,
                    "city": today_row.city,
                    "competitor_name": comp_name,
                    "position": comp_row.rank_position,
                    "message": f"{comp_name} newly entered the 3-pack for '{today_row.keyword}'",
                },
                triggered_at=datetime.now(timezone.utc),
            )
            db.add(alert)
            logger.warning(f"ALERT: Competitor entered pack — {comp_name} for '{today_row.keyword}' in {today_row.city}")
            _send_immediate_alert_email(alert, db)


def _send_immediate_alert_email(alert: Alert, db: Session) -> None:
    """Send an immediate alert email via Resend. Phase 5 wires this fully."""
    from app.config import settings
    if not settings.resend_api_key:
        logger.info("Resend not configured — skipping immediate alert email")
        return

    try:
        import resend
        resend.api_key = settings.resend_api_key

        if alert.alert_type == "pack_drop":
            subject = f"ALERT: Duncan Law dropped from 3-pack — {alert.detail.get('keyword')}"
            body = (
                f"<p><strong>Duncan Law has dropped out of the Google 3-pack.</strong></p>"
                f"<p><strong>Keyword:</strong> {alert.detail.get('keyword')}<br>"
                f"<strong>Market:</strong> {alert.market}<br>"
                f"<strong>Previous position:</strong> #{alert.detail.get('previous_position')}<br>"
                f"<strong>Current position:</strong> Not in pack</p>"
                f"<p><a href='{settings.app_base_url}/alerts'>View all alerts</a></p>"
            )
        else:
            subject = f"ALERT: Competitor entered 3-pack — {alert.detail.get('competitor_name')}"
            body = (
                f"<p><strong>{alert.detail.get('competitor_name')} has entered the Google 3-pack.</strong></p>"
                f"<p><strong>Keyword:</strong> {alert.detail.get('keyword')}<br>"
                f"<strong>Market:</strong> {alert.market}<br>"
                f"<strong>Position:</strong> #{alert.detail.get('position')}</p>"
                f"<p><a href='{settings.app_base_url}/alerts'>View all alerts</a></p>"
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


def check_pacer_trends(db: Session) -> None:
    """Phase 4: 90-day PACER filing trend alerts. Implemented in Phase 4."""
    pass
