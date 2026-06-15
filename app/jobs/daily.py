import logging
from datetime import datetime, timezone
from app.database import SessionLocal
from app.models.alerts import JobRun
from app.models.base import new_uuid

logger = logging.getLogger(__name__)


def run_daily_job() -> None:
    """Daily job: own firm rankings across all 24 keyword/city combos + alert checks."""
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

        from app.services.config_loader import get_own_firm_keywords
        from app.services.dataforseo import (
            collect_rankings_for_keywords, build_place_maps,
            collect_own_organic_rankings,
        )
        from app.services.alert_engine import check_pack_alerts, check_convergence_alerts
        from app.models.competitor import Competitor

        keywords = get_own_firm_keywords()
        own_firm_id, own_place_ids, competitor_place_map = build_place_maps(db)

        if not own_firm_id:
            logger.warning("Daily job: own firm not found in DB — skipping rankings")
        elif not own_place_ids:
            logger.warning("Daily job: own firm has no Google Place IDs configured — add them to competitors.yaml")
        else:
            records = collect_rankings_for_keywords(
                keywords=keywords,
                own_place_ids=own_place_ids,
                competitor_place_map=competitor_place_map,
                db=db,
                own_firm_id=own_firm_id,
                only_own_firm=True,
            )
            check_pack_alerts(db, own_firm_id)
            check_convergence_alerts(db, own_firm_id)

            # Organic rankings — track own firm's page-1 organic position daily
            own_firm = db.query(Competitor).filter(Competitor.id == own_firm_id).first()
            own_domain = own_firm.domain if own_firm and own_firm.domain else "duncanlawonline.com"
            records += collect_own_organic_rankings(
                keywords=keywords,
                own_domain=own_domain,
                db=db,
            )

        run.status = "success"
        run.records_processed = records
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(f"Daily job completed: {records} ranking rows stored")

    except Exception as e:
        run.status = "failed"
        run.error_detail = str(e)
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.error(f"Daily job failed: {e}", exc_info=True)
    finally:
        db.close()
