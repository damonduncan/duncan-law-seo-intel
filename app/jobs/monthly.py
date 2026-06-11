import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

ATTORNEY_JOBS = [
    # Consultations
    {"key": "damon", "email": "damonduncan@duncanlawonline.com",
     "cache_key": "consultation_monthly_damon", "event_type": "consult"},
    {"key": "anne",  "email": "anne@duncanlawonline.com",
     "cache_key": "consultation_monthly_anne",  "event_type": "consult"},
    # Signing appointments
    {"key": "damon", "email": "damonduncan@duncanlawonline.com",
     "cache_key": "signing_monthly_damon", "event_type": "signing"},
    {"key": "anne",  "email": "anne@duncanlawonline.com",
     "cache_key": "signing_monthly_anne",  "event_type": "signing"},
]


def _upsert_cache_month(db, cache_key: str, year: int, month: int, count: int,
                        notes: list = None) -> None:
    """Upsert a single month's count into a discovery_cache JSON blob."""
    from app.models.discovery import DiscoveryCache
    from app.models.base import new_uuid

    import json as _json
    row  = db.query(DiscoveryCache).filter(DiscoveryCache.key == cache_key).first()
    data = row.value if row else {"months": [], "notes": notes or []}
    if isinstance(data, str):
        data = _json.loads(data)

    months   = data.get("months", [])
    existing = next((m for m in months if m["year"] == year and m["month"] == month), None)
    if existing:
        existing["count"] = count
    else:
        months.append({"year": year, "month": month, "count": count})

    months.sort(key=lambda m: (m["year"], m["month"]))
    data["months"]     = months
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    if row:
        row.value      = data
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(DiscoveryCache(id=new_uuid(), key=cache_key, value=data,
                              updated_at=datetime.now(timezone.utc)))
    db.commit()


def run_monthly_consult_job() -> None:
    """Pull previous month's consultation, signing, and contract counts; update discovery_cache."""
    from app.database import SessionLocal
    from app.services.calendar_service import fetch_month_count

    today = date.today()
    if today.month == 1:
        target_year, target_month = today.year - 1, 12
    else:
        target_year, target_month = today.year, today.month - 1

    logger.info(f"Monthly consult job: pulling {target_year}-{target_month:02d}")

    db = SessionLocal()
    try:
        # ── Calendar pulls (consultations + signing appointments) ─────────────
        for atty in ATTORNEY_JOBS:
            try:
                count = fetch_month_count(
                    email=atty["email"],
                    attorney=atty["key"],
                    year=target_year,
                    month=target_month,
                    event_type=atty["event_type"],
                )
                _upsert_cache_month(db, atty["cache_key"], target_year, target_month, count)
                logger.info(f"Saved {atty['cache_key']} {target_year}-{target_month:02d} = {count}")
            except Exception as e:
                logger.error(
                    f"Monthly calendar pull failed for {atty['email']}: {e}",
                    exc_info=True,
                )

        # ── DocuSign attorney-client agreements ───────────────────────────────
        try:
            from app.services.docusign_service import fetch_contracts_count
            count = fetch_contracts_count(target_year, target_month)
            _upsert_cache_month(
                db, "docusign_monthly_contracts", target_year, target_month, count,
                notes=["DocuSign attorney-client agreements. Available from Dec 2022."],
            )
            logger.info(f"Saved docusign_monthly_contracts {target_year}-{target_month:02d} = {count}")
        except Exception as e:
            logger.error(f"DocuSign contract pull failed: {e}", exc_info=True)

    finally:
        db.close()
