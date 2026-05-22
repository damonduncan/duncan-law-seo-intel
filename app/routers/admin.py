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
        from app.services.pacer import (
            _find_form_with_password, _find_content_form,
            _all_inputs, _detect_field, _parse_result_count,
        )

        # Step 1: GET login page
        resp = session.get(PACER_LOGIN_URL, timeout=30)
        soup = BeautifulSoup(resp.text, "lxml")
        result["login_page_title"] = soup.title.string if soup.title else "(no title)"
        result["all_form_ids"] = [
            f.get("id") or f.get("name") or "(no id)"
            for f in soup.find_all("form")
        ]

        # Find the correct login form
        login_form = _find_form_with_password(soup)
        inputs = _all_inputs(login_form) if login_form else {}
        result["login_form_found"] = login_form is not None
        result["login_form_fields"] = list(inputs.keys())
        result["login_form_action"] = login_form.get("action", "(no action attr)") if login_form else None
        result["login_form_id"] = login_form.get("id", "(no id)") if login_form else None
        # Show non-empty hidden input values (redact password)
        result["hidden_input_values"] = {
            k: (v[:40] if k != "loginForm:password" else "***")
            for k, v in inputs.items() if v
        }

        # Detect field names dynamically
        user_field = _detect_field(login_form, ("text",), ("name", "login", "user")) if login_form else None
        pass_field = _detect_field(login_form, ("password",), ()) if login_form else None
        result["detected_user_field"] = user_field
        result["detected_pass_field"] = pass_field

        # Fix JSF form marker (name == value)
        form_id = login_form.get("id", "") if login_form else ""
        if form_id and form_id in inputs and not inputs[form_id]:
            inputs[form_id] = form_id

        if user_field:
            inputs[user_field] = settings.pacer_username
        if pass_field:
            inputs[pass_field] = settings.pacer_password

        from urllib.parse import urljoin as _urljoin
        form_action = login_form.get("action", "") if login_form else ""
        post_url = _urljoin(PACER_LOGIN_URL, form_action) if form_action else PACER_LOGIN_URL
        result["post_url"] = post_url

        form_id = login_form.get("id", "loginForm") if login_form else "loginForm"
        btn_id = f"{form_id}:fbtnLogin"
        inputs.update({
            "jakarta.faces.partial.ajax":    "true",
            "jakarta.faces.source":          btn_id,
            "jakarta.faces.partial.execute": "@all",
            "jakarta.faces.partial.render":  "@all",
            "jakarta.faces.behavior.event":  "action",
            "jakarta.faces.partial.event":   "click",
            btn_id:                          btn_id,
        })
        resp = session.post(
            post_url, data=inputs, timeout=30, allow_redirects=True,
            headers={
                "Referer": PACER_LOGIN_URL,
                "Origin": "https://pacer.login.uscourts.gov",
                "Content-Type": "application/x-www-form-urlencoded",
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/xml, text/xml, */*; q=0.01",
            },
        )
        result["login_status"] = resp.status_code
        result["login_content_type"] = resp.headers.get("Content-Type", "")
        result["login_raw_response"] = resp.text[:800].replace("\n", " ")

        from app.services.pacer import _parse_jsf_ajax_redirect
        result["ajax_redirect_url"] = _parse_jsf_ajax_redirect(resp.text)

        # Verify authentication via PCL (not the login.jsf redirect)
        pcl_check = session.get("https://pcl.uscourts.gov/pcl/index.jsf",
                                 timeout=30, headers={"Referer": post_url})
        pcl_soup = BeautifulSoup(pcl_check.text, "lxml")
        pcl_title = pcl_soup.title.string if pcl_soup.title else ""
        result["pcl_verify_title"] = pcl_title.strip()
        result["login_succeeded"] = "Welcome" in pcl_title or "PACER: Login" not in pcl_title
        result["login_response_snippet"] = pcl_check.text[:400].replace("\n", " ")

        # Step 2: GET PCL search page
        resp2 = session.get(PCL_SEARCH_PAGE, timeout=30)
        soup2 = BeautifulSoup(resp2.text, "lxml")
        result["pcl_page_title"] = soup2.title.string if soup2.title else "(no title)"
        result["pcl_all_form_ids"] = [
            f.get("id") or f.get("name") or "(no id)"
            for f in soup2.find_all("form")
        ]

        frm_search = soup2.find("form", id="frmSearch")
        content_form = frm_search or _find_content_form(soup2)
        pcl_inputs = _all_inputs(content_form) if content_form else {}
        result["pcl_frm_search_found"] = frm_search is not None
        result["pcl_form_id_used"] = content_form.get("id") if content_form else None
        result["pcl_form_fields"] = list(pcl_inputs.keys())

        # Step 3: POST search
        today = date.today()
        period_end = date(today.year, today.month, 1) - timedelta(days=1)
        period_start = date(period_end.year, period_end.month, 1)
        frm_id = content_form.get("id", "frmSearch") if content_form else "frmSearch"
        date_from = period_start.strftime("%m/%d/%Y")
        date_to   = period_end.strftime("%m/%d/%Y")
        pcl_inputs.update({
            f"{frm_id}:txtPartyNameLast":      last_name,
            f"{frm_id}:txtPartyNameFirst":     first_name,
            f"{frm_id}:txtPartyNameMiddle":    "",
            f"{frm_id}:scmPartyRole":          "at",
            f"{frm_id}:scmPartyRole_focus":    "",
            f"{frm_id}:scmPartyRole_filter":   "",
            f"{frm_id}:ddCaseTypeBasic_input": "bk",
            f"{frm_id}:cbExactMatches_input":  "false",
            f"{frm_id}:cbEmptyMatches_input":  "false",
            f"{frm_id}:courtId":               court_code,
            f"{frm_id}:dateFiledFrom":         date_from,
            f"{frm_id}:dateFiledTo":           date_to,
            f"{frm_id}:chapter":               str(chapter),
            f"{frm_id}:btnSearch":             "Search",
        })
        result["search_fields_submitted"] = list(pcl_inputs.keys())
        form_action2 = content_form.get("action", "") if content_form else ""
        from urllib.parse import urljoin as _uj2
        post_url2 = _uj2(resp2.url, form_action2) if form_action2 else resp2.url
        resp3 = session.post(post_url2, data=pcl_inputs, timeout=30,
                             headers={"Referer": resp2.url})
        resp3_soup = BeautifulSoup(resp3.text, "lxml")
        result["search_page_title"] = resp3_soup.title.string.strip() if resp3_soup.title else "(no title)"
        result["search_response_snippet"] = resp3.text[:800].replace("\n", " ")
        result["parsed_count"] = _parse_result_count(resp3.text)

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
