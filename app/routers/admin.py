import threading
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db

router = APIRouter()
auth_required = RedirectIfNotAuthenticated()
templates = Jinja2Templates(directory="app/templates")


@router.post("/admin/sync-config")
def sync_config(request: Request, user: dict = Depends(auth_required)):
    from app.database import SessionLocal
    from app.services.config_loader import sync_competitors
    db = SessionLocal()
    try:
        sync_competitors(db)
    finally:
        db.close()
    return RedirectResponse(url="/dashboard?msg=Config+synced+%E2%80%94+competitor+list+updated", status_code=303)


@router.post("/admin/run-reviews", response_class=HTMLResponse)
def run_reviews(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    """Run review collection synchronously and show a result page."""
    google_count, bbb_count, error = 0, 0, None
    try:
        from app.services.google_places import collect_competitor_reviews
        google_count = collect_competitor_reviews(db)
        from app.services.bbb import collect_bbb_reviews
        bbb_count = collect_bbb_reviews(db)
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse("admin_reviews_result.html", {
        "request": request,
        "user": user,
        "active_page": "reviews",
        "google_count": google_count,
        "bbb_count": bbb_count,
        "error": error,
    })


@router.post("/admin/run-pacer", response_class=HTMLResponse)
def run_pacer(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    """Run PACER filing collection synchronously and show a result page."""
    records, error = 0, None
    try:
        from app.services.pacer import collect_filing_snapshots
        records = collect_filing_snapshots(db)
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse("admin_pacer_result.html", {
        "request": request,
        "user": user,
        "active_page": "filings",
        "records": records,
        "error": error,
    })


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


@router.get("/admin/debug/pacer")
def debug_pacer(
    request: Request,
    last_name: str = "Duncan",
    first_name: str = "Damon",
    court_code: str = "ncmb",
    chapter: int = 7,
    user: dict = Depends(auth_required),
):
    """
    Test PACER login and run one PCL search. Shows raw HTML excerpts so
    we can see exactly what PACER returns and fix the parser if needed.
    """
    from app.config import settings
    import requests as req
    from bs4 import BeautifulSoup
    from datetime import date, timedelta

    result = {
        "credentials_set": bool(settings.pacer_username and settings.pacer_password),
        "pacer_username": settings.pacer_username or "(not set)",
        "search_params": {
            "last_name": last_name,
            "first_name": first_name,
            "court_code": court_code,
            "chapter": chapter,
        },
        "login_status": None,
        "login_page_title": None,
        "login_response_snippet": None,
        "login_form_fields": None,
        "pcl_page_title": None,
        "pcl_form_fields": None,
        "search_response_snippet": None,
        "parsed_count": None,
        "error": None,
    }

    PACER_LOGIN_URL = "https://pacer.login.uscourts.gov/csologin/login.jsf"
    PCL_SEARCH_PAGE = "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"

    if not settings.pacer_username:
        result["error"] = "PACER_USERNAME not configured in Railway env vars"
        return JSONResponse(result)

    session = req.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    try:
        # Step 1: GET login page
        resp = session.get(PACER_LOGIN_URL, timeout=30)
        soup = BeautifulSoup(resp.text, "lxml")
        result["login_page_title"] = soup.title.string if soup.title else "(no title)"

        # Collect all form inputs
        form = soup.find("form")
        inputs = {}
        if form:
            for inp in form.find_all("input"):
                if inp.get("name"):
                    inputs[inp["name"]] = inp.get("value", "")[:60]
        result["login_form_fields"] = list(inputs.keys())

        # Step 2: POST login
        inputs.update({
            "loginForm:loginName": settings.pacer_username,
            "loginForm:password": "***",
            "loginForm:clientCode": settings.pacer_client_code or "",
            "loginForm:fbtnLogin": "Login",
        })
        inputs_actual = dict(inputs)
        inputs_actual["loginForm:password"] = settings.pacer_password

        resp = session.post(PACER_LOGIN_URL, data=inputs_actual, timeout=30,
                            allow_redirects=True)
        result["login_status"] = resp.status_code
        result["login_response_snippet"] = resp.text[:800].replace("\n", " ")

        # Step 3: GET PCL search page
        resp2 = session.get(PCL_SEARCH_PAGE, timeout=30)
        soup2 = BeautifulSoup(resp2.text, "lxml")
        result["pcl_page_title"] = soup2.title.string if soup2.title else "(no title)"

        form2 = soup2.find("form")
        pcl_inputs = {}
        if form2:
            for inp in form2.find_all("input"):
                if inp.get("name"):
                    pcl_inputs[inp["name"]] = inp.get("value", "")[:60]
        result["pcl_form_fields"] = list(pcl_inputs.keys())

        # Step 4: POST search
        today = date.today()
        period_end = date(today.year, today.month, 1) - timedelta(days=1)
        period_start = date(period_end.year, period_end.month, 1)

        pcl_inputs.update({
            "findPartyForm:partyType": "at",
            "findPartyForm:lastName": last_name,
            "findPartyForm:firstName": first_name,
            "findPartyForm:courtType": "bk",
            "findPartyForm:courtId": court_code,
            "findPartyForm:dateFiled": (
                f"{period_start.strftime('%m/%d/%Y')} "
                f"to {period_end.strftime('%m/%d/%Y')}"
            ),
            "findPartyForm:chapter": str(chapter),
            "findPartyForm:btnSearch": "Search",
        })

        resp3 = session.post(PCL_SEARCH_PAGE, data=pcl_inputs, timeout=30)
        result["search_response_snippet"] = resp3.text[:1500].replace("\n", " ")

        # Try to parse count
        import re as re_mod
        from bs4 import BeautifulSoup as BS
        soup3 = BS(resp3.text, "lxml")
        paging = soup3.find(string=re_mod.compile(r"\d+\s+to\s+\d+\s+of\s+\d+", re_mod.IGNORECASE))
        if paging:
            m = re_mod.search(r"of\s+(\d[\d,]*)", paging)
            if m:
                result["parsed_count"] = int(m.group(1).replace(",", ""))
        if result["parsed_count"] is None:
            cnt = soup3.find(string=re_mod.compile(r"\d+\s+(result|case)", re_mod.IGNORECASE))
            if cnt:
                m = re_mod.search(r"(\d[\d,]*)", cnt)
                if m:
                    result["parsed_count"] = int(m.group(1).replace(",", ""))

    except Exception as e:
        result["error"] = str(e)

    return JSONResponse(result)


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
        "https://api.dataforseo.com/v3/serp/google/maps/live/advanced",
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
