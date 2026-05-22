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
    """Show raw DataForSEO response — used to debug Place ID matching and parser."""
    import requests
    from base64 import b64encode
    from app.config import settings
    from app.services.dataforseo import CITY_TO_LOCATION, _extract_city

    city = _extract_city(keyword) or "Greensboro"
    location_name = CITY_TO_LOCATION.get(city, "Greensboro,North Carolina,United States")

    token = b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    payload = [{"keyword": keyword, "location_name": location_name, "language_name": "English"}]

    resp = requests.post(
        "https://api.dataforseo.com/v3/serp/google/local_pack/live/regular",
        headers=headers, json=payload, timeout=30,
    )
    raw = resp.json()

    # Also extract just the items for easy inspection
    try:
        items = raw["tasks"][0]["result"][0]["items"]
    except Exception:
        items = []

    return JSONResponse({
        "keyword": keyword,
        "city": city,
        "location_name": location_name,
        "http_status": resp.status_code,
        "task_status_code": raw.get("tasks", [{}])[0].get("status_code"),
        "task_status_message": raw.get("tasks", [{}])[0].get("status_message"),
        "items_count": len(items),
        "items": items,
        "full_raw_response": raw,
    })
