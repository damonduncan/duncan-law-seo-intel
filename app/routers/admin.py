import threading
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from app.dependencies import RedirectIfNotAuthenticated

router = APIRouter()
auth_required = RedirectIfNotAuthenticated()


@router.post("/admin/run-job/daily")
def trigger_daily(request: Request, user: dict = Depends(auth_required)):
    from app.jobs.daily import run_daily_job
    thread = threading.Thread(target=run_daily_job, daemon=True)
    thread.start()
    return RedirectResponse(url="/dashboard?msg=daily+job+started", status_code=303)


@router.post("/admin/run-job/weekly")
def trigger_weekly(request: Request, user: dict = Depends(auth_required)):
    from app.jobs.weekly import run_weekly_job
    thread = threading.Thread(target=run_weekly_job, daemon=True)
    thread.start()
    return RedirectResponse(url="/dashboard?msg=weekly+job+started", status_code=303)


@router.get("/admin/debug/pack")
def debug_pack(
    request: Request,
    keyword: str = "bankruptcy attorney Greensboro",
    user: dict = Depends(auth_required),
):
    """Show raw DataForSEO response for one keyword — used to debug Place ID matching."""
    from app.services.dataforseo import fetch_local_pack, _extract_city, build_place_maps
    from app.database import SessionLocal

    city = _extract_city(keyword)
    results = fetch_local_pack(keyword, city or "Greensboro")

    db = SessionLocal()
    try:
        own_firm_id, own_place_ids, competitor_place_map = build_place_maps(db)
    finally:
        db.close()

    return JSONResponse({
        "keyword": keyword,
        "city": city,
        "own_place_ids_stored": own_place_ids,
        "results_from_dataforseo": [
            {
                "rank": r.get("rank_position"),
                "title": r.get("title"),
                "place_id": r.get("place_id"),
                "matched_as_own_firm": r.get("place_id") in own_place_ids,
                "matched_as_competitor": r.get("place_id") in competitor_place_map,
            }
            for r in results
        ],
    })
