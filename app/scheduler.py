import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(max_workers=2)},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
)


def register_jobs() -> None:
    from app.jobs.daily import run_daily_job
    from app.jobs.weekly import run_weekly_job

    # Daily at 6:00 AM ET — own rankings, own GBP, alert checks
    scheduler.add_job(
        run_daily_job,
        trigger="cron",
        hour=6,
        minute=0,
        timezone="America/New_York",
        id="daily_job",
        name="Daily: own rankings + alerts",
        replace_existing=True,
    )

    # Weekly Monday at 5:00 AM ET — competitor data, trends, digest
    scheduler.add_job(
        run_weekly_job,
        trigger="cron",
        day_of_week="mon",
        hour=5,
        minute=0,
        timezone="America/New_York",
        id="weekly_job",
        name="Weekly: competitor data + digest",
        replace_existing=True,
    )

    logger.info("Scheduler jobs registered: daily (6 AM ET), weekly (Mon 5 AM ET)")
