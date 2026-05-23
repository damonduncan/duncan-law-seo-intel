import threading
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.config import settings

router = APIRouter()
auth_required = RedirectIfNotAuthenticated()
templates = Jinja2Templates(directory="app/templates")


@router.post("/admin/clear-filings")
def clear_filings(request: Request, user: dict = Depends(auth_required), db: Session = Depends(get_db)):
    """Delete all filing snapshots so bad data doesn't persist on the Filings page."""
    from app.models.filings import FilingSnapshot
    deleted = db.query(FilingSnapshot).delete()
    db.commit()
    return RedirectResponse(
        url="/dashboard?msg=Cleared+{d}+filing+snapshots".format(d=deleted),
        status_code=303,
    )


@router.post("/admin/clean-pacer-dupes")
def clean_pacer_dupes(request: Request, user: dict = Depends(auth_required), db: Session = Depends(get_db)):
    """
    Remove duplicate FilingSnapshot rows — keeps the highest case_count per
    (competitor_id, attorney_id, district, chapter, period_start) combination.
    Fixes the 0-row / real-count-row display issue.
    """
    from app.models.filings import FilingSnapshot
    from sqlalchemy import func

    # Find all duplicates and keep only the row with the highest case_count
    all_snaps = db.query(FilingSnapshot).all()
    seen: dict = {}
    to_delete = []
    for s in all_snaps:
        key = (s.competitor_id, s.attorney_id, s.district, s.chapter, s.period_start)
        if key not in seen:
            seen[key] = s
        else:
            # Keep whichever has the higher count; mark the other for deletion
            if s.case_count > seen[key].case_count:
                to_delete.append(seen[key].id)
                seen[key] = s
            else:
                to_delete.append(s.id)

    deleted = 0
    for snap_id in to_delete:
        db.query(FilingSnapshot).filter(FilingSnapshot.id == snap_id).delete()
        deleted += 1
    db.commit()
    return RedirectResponse(
        url="/filings?msg=removed_{d}_duplicate_rows".format(d=deleted),
        status_code=303,
    )


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


@router.post("/admin/discover/ednc")
def discover_ednc(
    request: Request,
    user: dict = Depends(auth_required),
    year: int = 2026,
    month: int = 4,
):
    """Start EDNC top-filer discovery in background. Results stored in DB."""
    from app.database import SessionLocal
    from app.services.pacer_discovery import run_ednc_discovery

    def _run():
        db = SessionLocal()
        try:
            run_ednc_discovery(db, year=year, month=month)
        finally:
            db.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return RedirectResponse(url="/filings?msg=discovery_running", status_code=303)


@router.get("/admin/discover/ednc-filers")
def discover_ednc_filers(
    request: Request,
    user: dict = Depends(auth_required),
    year: int = 2026,
    month: int = 4,
):
    """
    Pull the NCEB (Eastern District NC Bankruptcy) Filed Cases report
    for a given month and return a ranked list of attorneys by case count.
    Use this to discover who the major EDNC bankruptcy filers are so
    they can be added to competitors.yaml.
    """
    from playwright.sync_api import sync_playwright
    from datetime import date
    import calendar
    import re as _re
    from collections import Counter

    period_start = date(year, month, 1)
    period_end   = date(year, month, calendar.monthrange(year, month)[1])
    court_base   = "https://ecf.nceb.uscourts.gov"

    result: dict = {
        "period": f"{period_start.strftime('%B %Y')}",
        "login_ok": False,
        "court_ok": False,
        "report_title": None,
        "top_filers": [],
        "total_cases_scanned": 0,
        "error": None,
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            page.set_default_timeout(45_000)

            # Central login
            page.goto("https://pacer.login.uscourts.gov/csologin/login.jsf", wait_until="domcontentloaded")
            page.fill('[name="loginForm:loginName"]', settings.pacer_username)
            page.fill('[name="loginForm:password"]',  settings.pacer_password)
            page.click('[id$="fbtnLogin"]')
            page.wait_for_timeout(4_000)
            result["login_ok"] = True

            # NCEB court handoff
            page.goto(f"{court_base}/cgi-bin/login.pl", wait_until="domcontentloaded")
            court_title = page.title()
            result["court_ok"] = "Login" not in court_title and bool(court_title)
            if not result["court_ok"]:
                result["error"] = f"NCEB auth failed — title: {court_title!r}"
                browser.close()
                return JSONResponse(result)

            # CaseFiled-Rpt.pl is NCEB's "Cases Filed" report (confirmed from Reports menu)
            report_url = f"{court_base}/cgi-bin/CaseFiled-Rpt.pl"
            page.goto(report_url, wait_until="domcontentloaded")
            result["report_title"] = page.title()
            result["report_url_used"] = report_url

            # Capture form fields so we know what parameters to use
            form_fields = page.eval_on_selector_all(
                "input, select",
                "els => els.map(e => ({name: e.name, id: e.id, type: e.type}))"
            )
            result["report_form_fields"] = form_fields[:20]

            # Fill date range — try common field names used in CM/ECF report forms
            date_from_str = period_start.strftime("%m/%d/%Y")
            date_to_str   = period_end.strftime("%m/%d/%Y")
            for fname in ["date_from", "filed_from", "Sdate", "DateFiled_from", "start_date"]:
                try:
                    page.fill(f'[name="{fname}"]', date_from_str, timeout=1_500)
                    break
                except Exception:
                    pass
            for fname in ["date_to", "filed_to", "Edate", "DateFiled_to", "end_date"]:
                try:
                    page.fill(f'[name="{fname}"]', date_to_str, timeout=1_500)
                    break
                except Exception:
                    pass

            # Select bankruptcy case type if available
            for sel_name in ["case_type", "caseType", "type"]:
                try:
                    page.select_option(f'[name="{sel_name}"]', value="bk", timeout=1_500)
                    break
                except Exception:
                    pass

            # Submit
            try:
                page.click('input[type="submit"], input[name="button1"], button[type="submit"]',
                           timeout=5_000)
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                pass

            result["result_title"]   = page.title()
            body = page.inner_text("body")
            result["result_snippet"] = body[:3000]

            # Extract attorney names — CM/ECF marks attorneys as "(aty)" after their name
            atty_pattern = _re.compile(r'([A-Z][A-Za-z\-\'\.]+,\s+[A-Za-z][A-Za-z\s\.]+)\s*\(aty\)')
            names = atty_pattern.findall(body)
            result["total_cases_scanned"] = len(names)
            counter = Counter(n.strip() for n in names)
            result["top_filers"] = [
                {"attorney": name, "cases": count}
                for name, count in counter.most_common(30)
            ]

            browser.close()

    except Exception as e:
        result["error"] = str(e)

    return JSONResponse(result)


@router.get("/admin/debug/cmecf")
def debug_cmecf(
    request: Request,
    court_code: str = "ncmb",
    last_name: str = "Duncan",
    first_name: str = "Damon",
    chapter: int = 7,
    user: dict = Depends(auth_required),
):
    """
    After PACER login, navigate to the CM/ECF court and inspect the
    attorney query interface — shows what forms/links/fields are available.
    """
    from playwright.sync_api import sync_playwright
    from datetime import date, timedelta

    result: dict = {
        "login_title": None,
        "post_login_url": None,
        "cmecf_title": None,
        "cmecf_url": None,
        "loginpl_title": None,
        "loginpl_url": None,
        "nav_links": None,
        "iquery_title": None,
        "iquery_form_fields": None,
        "iquery_html_snippet": None,
        "search_result_title": None,
        "search_result_snippet": None,
        "error": None,
    }

    today      = date.today()
    period_end   = date(today.year, today.month, 1) - timedelta(days=1)
    period_start = date(period_end.year, period_end.month, 1)
    court_base = f"https://ecf.{court_code}.uscourts.gov"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            page.set_default_timeout(45_000)

            # Step 1: Central PACER login (plain — no court selector, less fragile)
            page.goto("https://pacer.login.uscourts.gov/csologin/login.jsf",
                      wait_until="domcontentloaded")
            page.fill('[name="loginForm:loginName"]', settings.pacer_username)
            page.fill('[name="loginForm:password"]',  settings.pacer_password)
            page.click('[id$="fbtnLogin"]')
            page.wait_for_timeout(4_000)   # wait for AJAX redirect cycle
            result["login_title"]    = page.title()
            result["post_login_url"] = page.url

            # Step 2: Navigate to CM/ECF court homepage (use domcontentloaded —
            # networkidle fails because CM/ECF does protocol-level redirects)
            try:
                page.goto(court_base, wait_until="domcontentloaded", timeout=30_000)
                result["cmecf_title"] = page.title()
                result["cmecf_url"]   = page.url
            except Exception as nav_err:
                result["cmecf_title"] = f"nav error: {nav_err}"

            # Step 3: CM/ECF login.pl handoff — this endpoint exchanges PACER
            # central auth cookies for a court-specific session token
            login_pl = f"{court_base}/cgi-bin/login.pl"
            try:
                page.goto(login_pl, wait_until="domcontentloaded", timeout=30_000)
                result["loginpl_title"] = page.title()
                result["loginpl_url"]   = page.url
            except Exception as lp_err:
                result["loginpl_title"] = f"nav error: {lp_err}"

            # Step 4: Now try iquery.pl
            iquery_url = f"{court_base}/cgi-bin/iquery.pl"
            try:
                page.goto(iquery_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as iq_err:
                result["iquery_nav_error"] = str(iq_err)
            result["iquery_title"] = page.title()

            # Capture form fields
            form_fields = page.eval_on_selector_all(
                "input, select",
                "els => els.map(e => ({tag: e.tagName, name: e.name, id: e.id, type: e.type}))"
            )
            result["iquery_form_fields"] = form_fields[:30]
            result["iquery_html_snippet"] = page.content()[:3000]

            # Capture select options for person_type and nature_suit
            result["person_type_options"] = page.eval_on_selector_all(
                '[name="person_type"] option',
                "els => els.map(e => ({value: e.value, text: e.innerText.trim()}))"
            )
            result["nature_suit_options"] = page.eval_on_selector_all(
                '[name="nature_suit"] option',
                "els => els.map(e => ({value: e.value, text: e.innerText.trim()}))"
            )

            # Test attorney search using correct field names
            try:
                page.fill('[name="last_name"]',  last_name)
                page.fill('[name="first_name"]', first_name)
                page.fill('[name="filed_from"]', period_start.strftime("%m/%d/%Y"))
                page.fill('[name="filed_to"]',   period_end.strftime("%m/%d/%Y"))

                # Select Attorney in person_type
                try:
                    page.select_option('[name="person_type"]', value="aty", timeout=2_000)
                except Exception:
                    try:
                        page.select_option('[name="person_type"]', label="Attorney", timeout=2_000)
                    except Exception:
                        pass

                # Select chapter in nature_suit
                for val in ([str(chapter), f"bk{chapter}", f"0{chapter}" if chapter < 10 else str(chapter)]):
                    try:
                        page.select_option('[name="nature_suit"]', value=val, timeout=1_000)
                        break
                    except Exception:
                        pass

                # Check both open and closed cases
                for cb in ["open_cases", "closed_cases"]:
                    try:
                        if not page.is_checked(f'#{cb}'):
                            page.check(f'#{cb}', timeout=1_000)
                    except Exception:
                        pass

                # Submit — the button is type="button" named "button1"
                page.click('[name="button1"]', timeout=5_000)
                page.wait_for_load_state("domcontentloaded")
                result["search_result_title"]   = page.title()
                result["search_result_snippet"] = page.inner_text("body")[:3000]
            except Exception as e:
                result["search_result_title"] = f"Search attempt failed: {e}"

            browser.close()

    except Exception as e:
        result["error"] = str(e)

    return JSONResponse(result)


@router.get("/admin/debug/pacer-playwright")
def debug_pacer_playwright(
    request: Request,
    last_name: str = "Duncan",
    first_name: str = "Damon",
    user: dict = Depends(auth_required),
):
    """
    Run a full Playwright PACER session and return the rendered HTML of
    the search results page and refinement form — shows exactly what
    JavaScript renders so we can write the right selectors.
    """
    from playwright.sync_api import sync_playwright
    from app.config import settings

    result: dict = {
        "login_title": None,
        "search_title": None,
        "refine_form_html": None,
        "refine_form_inputs": None,
        "results_page_text_snippet": None,
        "error": None,
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.set_default_timeout(45_000)

            # Login
            page.goto("https://pacer.login.uscourts.gov/csologin/login.jsf",
                      wait_until="networkidle")
            page.fill('[name="loginForm:loginName"]', settings.pacer_username)
            page.fill('[name="loginForm:password"]',  settings.pacer_password)
            page.click('[id$="fbtnLogin"]')
            page.wait_for_load_state("networkidle")
            result["login_title"] = page.title()

            # Search
            page.goto("https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf",
                      wait_until="networkidle")
            page.fill('[name="frmSearch:txtPartyNameLast"]',  last_name)
            page.fill('[name="frmSearch:txtPartyNameFirst"]', first_name)
            try:
                page.select_option('[name="frmSearch:scmPartyRole"]',
                                   label="Attorney", timeout=5_000)
            except Exception:
                pass
            page.click('[id$="btnSearch"]')
            page.wait_for_load_state("networkidle")
            result["search_title"] = page.title()

            # Capture body text (first 2000 chars) for count pattern analysis
            result["results_page_text_snippet"] = page.inner_text("body")[:2000]

            # Capture the rendered refinement form HTML
            refine = page.locator("form#frmRefineSearch")
            if refine.count() > 0:
                result["refine_form_html"] = refine.inner_html()[:4000]
                # Also capture all input/select names + types
                inputs = page.eval_on_selector(
                    "form#frmRefineSearch",
                    """el => Array.from(el.querySelectorAll('input,select,button'))
                        .map(e => ({tag: e.tagName, name: e.name, id: e.id,
                                    type: e.type, visible: e.offsetParent !== null}))""",
                )
                result["refine_form_inputs"] = inputs
            else:
                result["refine_form_html"] = "(frmRefineSearch not found on page)"

            browser.close()

    except Exception as e:
        result["error"] = str(e)

    return JSONResponse(result)


@router.post("/admin/send-digest", response_class=HTMLResponse)
def send_digest(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    """Send the weekly digest email immediately."""
    error = None
    sent  = False
    try:
        from app.services.email_digest import build_and_send_digest
        build_and_send_digest(db)
        sent = True
    except Exception as e:
        error = str(e)
    return templates.TemplateResponse("admin_digest_result.html", {
        "request": request,
        "user":    user,
        "active_page": "dashboard",
        "sent":  sent,
        "error": error,
        "recipient": settings.digest_recipient,
    })


@router.post("/admin/run-pacer")
def run_pacer(request: Request, user: dict = Depends(auth_required)):
    """Start PACER collection in a background thread — returns immediately."""
    from datetime import datetime, timezone
    from app.database import SessionLocal
    from app.services.pacer import collect_filing_snapshots
    from app.models.alerts import JobRun
    from app.models.base import new_uuid

    # Create job record so the status endpoint can report progress
    db = SessionLocal()
    try:
        job = JobRun(
            id=new_uuid(),
            job_name="pacer",
            started_at=datetime.now(timezone.utc),
            status="running",
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    def _run():
        db2 = SessionLocal()
        try:
            records = collect_filing_snapshots(db2)
            job2 = db2.query(JobRun).filter(JobRun.id == job_id).first()
            if job2:
                job2.status = "success"
                job2.records_processed = records
                job2.completed_at = datetime.now(timezone.utc)
                db2.commit()
        except Exception as e:
            job2 = db2.query(JobRun).filter(JobRun.id == job_id).first()
            if job2:
                job2.status = "failed"
                job2.error_detail = str(e)
                job2.completed_at = datetime.now(timezone.utc)
                db2.commit()
        finally:
            db2.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return RedirectResponse(url="/filings?msg=pacer_running", status_code=303)


@router.get("/api/pacer-status")
def pacer_status(request: Request, user: dict = Depends(auth_required)):
    """Return the status of the most recent PACER collection job."""
    from datetime import datetime, timezone
    from app.database import SessionLocal
    from app.models.alerts import JobRun

    db = SessionLocal()
    try:
        job = (
            db.query(JobRun)
            .filter(JobRun.job_name == "pacer")
            .order_by(JobRun.started_at.desc())
            .first()
        )
        if not job:
            return JSONResponse({"status": "never_run"})

        now = datetime.now(timezone.utc)
        elapsed = int((now - job.started_at.replace(tzinfo=timezone.utc)).total_seconds())
        # If still "running" after 25 min the browser likely crashed silently
        status = job.status
        if status == "running" and elapsed > 1500:
            status = "stalled"

        return JSONResponse({
            "status":            status,
            "started_at":        job.started_at.isoformat(),
            "elapsed_seconds":   elapsed,
            "records_processed": job.records_processed,
            "error":             job.error_detail,
        })
    finally:
        db.close()


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
    import re as _re
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
        # Show 4000 chars so we can see the count text and filter elements
        result["search_response_snippet"] = resp3.text[:4000].replace("\n", " ")
        result["parsed_count"] = _parse_result_count(resp3.text)
        # Show what forms are on the results page (for filter options)
        result["results_form_ids"] = [
            f.get("id") or f.get("name") or "(no id)"
            for f in resp3_soup.find_all("form")
        ]
        # Find any text nodes containing "of" + number (to see raw count text)
        count_texts = []
        for el in resp3_soup.find_all(string=_re.compile(r"\d+\s+to\s+\d+\s+of\s+\d+|\d+\s+result|\d+\s+case", _re.I)):
            count_texts.append(el.strip()[:120])
        result["count_text_found"] = count_texts[:5]
        # Show party role options from the search form to find correct attorney code
        role_options = []
        if frm_search:
            role_sel = frm_search.find(attrs={"name": _re.compile("scmPartyRole$")})
            if role_sel:
                for opt in role_sel.find_all("option"):
                    role_options.append({"value": opt.get("value"), "text": opt.text.strip()})
        result["party_role_options"] = role_options[:20]
        # Inspect frmRefineSearch on the results page — this is how we filter by court/date/chapter
        refine_form = resp3_soup.find("form", id="frmRefineSearch")
        if refine_form:
            result["frmRefineSearch_fields"] = list(_all_inputs(refine_form).keys())
            result["frmRefineSearch_action"] = refine_form.get("action", "(none)")
            # Show select options in refine form
            refine_selects = {}
            for sel in refine_form.find_all("select"):
                name = sel.get("name", "")
                opts = [{"value": o.get("value"), "text": o.text.strip()[:40]} for o in sel.find_all("option")]
                refine_selects[name] = opts[:15]
            result["frmRefineSearch_select_options"] = refine_selects
        else:
            result["frmRefineSearch_fields"] = None

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
