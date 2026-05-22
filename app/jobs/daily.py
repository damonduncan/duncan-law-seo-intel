import logging
from datetime import datetime, timezone
from app.database import SessionLocal
from app.models.alerts import JobRun
from app.models.base import new_uuid

logger = logging.getLogger(__name__)


def run_daily_job() -> None:
    """Daily job: own rankings, own GBP snapshot, alert condition checks."""
    db = SessionLocal()
    run = JobRun(
        id=new_uuid(),
        job_name="daily",
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(run)
    db.commit()

    try:
        records = 0

        # Phase 2: DataForSEO own-firm rankings
        # from app.services.dataforseo import collect_own_rankings
        # records += collect_own_rankings(db)

        # Phase 3: Own GBP snapshot
        # from app.services.google_business import collect_own_gbp
        # records += collect_own_gbp(db)

        # Phase 2: Alert checks
        # from app.services.alert_engine import check_pack_alerts
        # check_pack_alerts(db)

        run.status = "success"
        run.records_processed = records
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(f"Daily job completed: {records} records processed")

    except Exception as e:
        run.status = "failed"
        run.error_detail = str(e)
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.error(f"Daily job failed: {e}", exc_info=True)
    finally:
        db.close()
