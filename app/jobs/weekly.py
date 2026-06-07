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

        # Phase 3: Competitor reviews + sentiment analysis
        from app.services.google_places import collect_competitor_reviews
        records += collect_competitor_reviews(db)
        from app.services.alert_engine import check_review_gaps
        check_review_gaps(db)
        from app.services.review_sentiment import analyze_competitor_sentiment
        analyze_competitor_sentiment(db)

        # Phase 4: PACER (only on 1st of month)
        if date.today().day == 1:
            from app.services.pacer import collect_filing_snapshots
            records += collect_filing_snapshots(db)
            from app.services.alert_engine import check_pacer_trends
            check_pacer_trends(db)

            # Discovery for all 3 districts — runs for the month that just closed.
            # Each district takes ~10 min (Playwright), so run in daemon threads
            # mirroring the /admin/discover/{district} endpoint pattern.
            import threading as _threading
            from app.database import SessionLocal as _SessionLocal
            from app.services.pacer_discovery import run_district_discovery as _run_district_discovery

            _today = date.today()
            disc_month = _today.month - 1 if _today.month > 1 else 12
            disc_year  = _today.year      if _today.month > 1 else _today.year - 1

            def _discover(district: str, year: int, month: int) -> None:
                _db = _SessionLocal()
                try:
                    _run_district_discovery(_db, district=district, year=year, month=month)
                except Exception as _e:
                    logger.error(f"Discovery thread failed for {district}: {_e}", exc_info=True)
                finally:
                    _db.close()

            for _district in ("MDNC", "WDNC", "EDNC"):
                _threading.Thread(
                    target=_discover,
                    args=(_district, disc_year, disc_month),
                    daemon=True,
                ).start()
            logger.info(
                f"PACER discovery threads started for {disc_year}-{disc_month:02d}: MDNC, WDNC, EDNC"
            )

            # Monthly database backup — email gzipped JSON to admin
            from app.services.backup import run_backup
            run_backup(db)

            # Monthly GA4 traffic pull
            try:
                from app.services.ga_service import run_ga_pull
                ga_fetched = run_ga_pull(db)
                logger.info(f"GA4 pull complete: {ga_fetched} months fetched")
            except Exception as _ga_e:
                logger.error(f"GA4 pull failed: {_ga_e}", exc_info=True)

        # Phase 5: Send weekly digest
        from app.services.email_digest import build_and_send_digest
        build_and_send_digest(db)

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
