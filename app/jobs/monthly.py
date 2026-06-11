import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

ATTORNEYS = [
    {
        "key":       "damon",
        "email":     "damonduncan@duncanlawonline.com",
        "cache_key": "consultation_monthly_damon",
    },
    {
        "key":       "anne",
        "email":     "anne@duncanlawonline.com",
        "cache_key": "consultation_monthly_anne",
    },
]


def run_monthly_consult_job() -> None:
    """Pull previous month's consultation counts for all attorneys and update discovery_cache."""
    from app.database import SessionLocal
    from app.models.discovery import DiscoveryCache
    from app.models.base import new_uuid
    from app.services.calendar_service import fetch_month_count

    today = date.today()
    if today.month == 1:
        target_year, target_month = today.year - 1, 12
    else:
        target_year, target_month = today.year, today.month - 1

    logger.info(f"Monthly consult job: pulling {target_year}-{target_month:02d}")

    db = SessionLocal()
    try:
        for atty in ATTORNEYS:
            try:
                count = fetch_month_count(
                    email=atty["email"],
                    attorney=atty["key"],
                    year=target_year,
                    month=target_month,
                )

                row  = db.query(DiscoveryCache).filter(
                    DiscoveryCache.key == atty["cache_key"]
                ).first()
                data = row.value if row else {"months": [], "notes": []}

                months = data.get("months", [])
                existing = next(
                    (m for m in months
                     if m["year"] == target_year and m["month"] == target_month),
                    None,
                )
                if existing:
                    existing["count"] = count
                else:
                    months.append({"year": target_year, "month": target_month, "count": count})

                months.sort(key=lambda m: (m["year"], m["month"]))
                data["months"]     = months
                data["updated_at"] = datetime.now(timezone.utc).isoformat()

                if row:
                    row.value      = data
                    row.updated_at = datetime.now(timezone.utc)
                else:
                    db.add(DiscoveryCache(
                        id=new_uuid(),
                        key=atty["cache_key"],
                        value=data,
                        updated_at=datetime.now(timezone.utc),
                    ))

                db.commit()
                logger.info(f"Saved {atty['cache_key']} {target_year}-{target_month:02d} = {count}")

            except Exception as e:
                logger.error(
                    f"Monthly consult pull failed for {atty['email']}: {e}",
                    exc_info=True,
                )
    finally:
        db.close()
