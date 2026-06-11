import json
import re
import logging
import calendar as cal_module
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Consultation filters ──────────────────────────────────────────────────────
CONSULT_INCLUDE = re.compile(r'consult|appointment booked', re.IGNORECASE)
CONSULT_EXCLUDE = re.compile(
    r'\[SKIP\]|\bCANCEL(ED|L?ED)?\b|\*cancel\w*\*|\bNO[\s-]?SHOW\b'
    r'|\[NOT COMING\]|\*NOT COMING\*|\[RESCHEDUL|\bRESCHEDUL'
    r'|\bsit in\b|\bhold for\b|\bsigning\b',   # exclude signing events from consult count
    re.IGNORECASE
)

# ── Signing appointment filters ───────────────────────────────────────────────
SIGNING_INCLUDE = re.compile(r'\bsigning\b', re.IGNORECASE)
SIGNING_EXCLUDE = re.compile(
    r'\[SKIP\]|\bCANCEL(ED|L?ED)?\b|\*cancel\w*\*|\bNO[\s-]?SHOW\b'
    r'|\[NOT COMING\]|\*NOT COMING\*|\[RESCHEDUL|\bRESCHEDUL',
    re.IGNORECASE
)

TMD = re.compile(r'\bTMD\b', re.IGNORECASE)


def _get_service(subject_email: str):
    from googleapiclient.discovery import build
    from google.oauth2 import service_account
    from app.config import settings

    raw = settings.calendar_credentials_json
    if not raw:
        raise ValueError("CALENDAR_CREDENTIALS_JSON env var is not set")

    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    ).with_subject(subject_email)

    return build("calendar", "v3", credentials=creds)


def _is_valid_event(ev: dict, attorney: str, event_type: str = "consult") -> bool:
    if ev.get("status") == "cancelled":
        return False

    title = ev.get("summary", "")

    if event_type == "signing":
        if not SIGNING_INCLUDE.search(title):
            return False
        if SIGNING_EXCLUDE.search(title):
            return False
        end_hour = 22          # signing appointments can run until 10pm
    else:
        if not CONSULT_INCLUDE.search(title):
            return False
        if CONSULT_EXCLUDE.search(title):
            return False
        end_hour = 18          # consultations are 8am–6pm only

    start = ev.get("start", {})
    dt_str = start.get("dateTime") or start.get("date")
    if not dt_str or "T" not in dt_str:
        return False  # all-day event — skip

    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
        dt   = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(eastern)
        mins = dt.hour * 60 + dt.minute
        if mins < 8 * 60 or mins >= end_hour * 60:
            return False
    except Exception:
        return False

    if attorney == "damon":
        if event_type == "consult" and TMD.search(title):
            return False
        if ev.get("creator", {}).get("email") == "terryduncan@duncanlawonline.com":
            return False

    return True


def fetch_month_count(email: str, attorney: str, year: int, month: int,
                      event_type: str = "consult") -> int:
    """Return filtered event count for one attorney/month via Calendar API.

    event_type: 'consult' or 'signing'
    """
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo("America/New_York")

    first    = datetime(year, month, 1, tzinfo=eastern)
    last_day = cal_module.monthrange(year, month)[1]
    last     = datetime(year, month, last_day, 23, 59, 59, tzinfo=eastern)

    service    = _get_service(email)
    count      = 0
    seen       = set()
    page_token = None

    while True:
        params = dict(
            calendarId=email,
            timeMin=first.isoformat(),
            timeMax=last.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        )
        if page_token:
            params["pageToken"] = page_token

        result = service.events().list(**params).execute()
        for ev in result.get("items", []):
            eid = ev.get("id", "")
            if eid in seen:
                continue
            seen.add(eid)
            if _is_valid_event(ev, attorney, event_type):
                count += 1

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"Calendar pull [{event_type}] {email} {year}-{month:02d}: {count}")
    return count
