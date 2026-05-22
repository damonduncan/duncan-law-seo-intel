import logging
from datetime import datetime, timezone, date
from app.database import SessionLocal
from app.models.alerts import JobRun
from app.models.base import new_uuid

logger = logging.getLogger(__name__)


def run_weekly_job() -> None:
    """Weekly job: all competitor rankings, reviews, PACER (1st of month), trends, digest."""
    db = SessionLocal()
    run = JobRun(
        id=new_uuid(),
        job_name="weekly",
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(run)
    db.commit()

    try:
        records = 0

        from app.services.config_loader import get_keywords
        from app.services.dataforseo import collect_rankings_for_keywords, build_place_maps

        keywords = get_keywords()
        own_firm_id, own_place_ids, competitor_place_map = build_place_maps(db)

        if not own_firm_id:
            logger.warning("Weekly job: own firm not found in DB")
        else:
            records += collect_rankings_for_keywords(
                keywords=keywords,
                own_place_ids=own_place_ids,
                competitor_place_map=competitor_place_map,
                db=db,
                own_firm_id=own_firm_id,
                only_own_firm=False,
            )

        # Phase 3: Competitor reviews
        # from app.services.google_places import collect_competitor_reviews
        # records += collect_competitor_reviews(db)
        # from app.services.bbb import collect_bbb_reviews
        # records += collect_bbb_reviews(db)

        # Phase 4: PACER (only on 1st of month)
        # if date.today().day == 1:
        #     from app.services.pacer import collect_filing_snapshots
        #     records += collect_filing_snapshots(db)
        #     from app.services.alert_engine import check_pacer_trends
        #     check_pacer_trends(db)

        # Phase 5: Send weekly digest
        # from app.services.email_digest import build_and_send_digest
        # build_and_send_digest(db)

        run.status = "success"
        run.records_processed = records
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(f"Weekly job completed: {records} records processed")

    except Exception as e:
        run.status = "failed"
        run.error_detail = str(e)
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.error(f"Weekly job failed: {e}", exc_info=True)
    finally:
        db.close()
