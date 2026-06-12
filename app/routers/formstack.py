"""Formstack integration — syncs intake form submissions and aggregates referral sources."""

import json
import logging
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import RedirectIfNotAuthenticated
from app.models.base import new_uuid
from app.models.discovery import DiscoveryCache

logger = logging.getLogger(__name__)
router = APIRouter()
auth_required = RedirectIfNotAuthenticated()

_API_BASE   = "https://www.formstack.com/api/v2"
_FIELD_ID   = "95584661"   # "How did you hear about us?" checkbox field
_CACHE_KEY  = "formstack_referral_sources"
_TTL_HOURS  = 6


# ── Helpers ─────────────────────────────────────────────────────────────────

def _fs_get(path: str) -> dict:
    token = settings.formstack_token
    req = urllib.request.Request(
        f"{_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _normalize(raw: str) -> str:
    raw = raw.strip()
    if raw.lower().startswith("other:"):
        return "Other / Returning"
    return raw


def _fetch_submissions() -> list[dict]:
    form_id = settings.formstack_form_id
    subs, page = [], 1
    while True:
        data  = _fs_get(
            f"/form/{form_id}/submission.json"
            f"?per_page=25&page={page}&sort=ASC&data=true&expand_data=true"
        )
        batch = data.get("submissions", [])
        subs.extend(batch)
        if page >= int(data.get("pages", 1)):
            break
        page += 1
    return subs


def _aggregate(subs: list[dict]) -> dict:
    all_time: Counter          = Counter()
    monthly:  dict[str, Counter] = defaultdict(Counter)

    for s in subs:
        val = s.get("data", {}).get(_FIELD_ID, {}).get("value", [])
        if isinstance(val, str):
            val = [val]
        month = (s.get("timestamp") or "")[:7]
        for v in val:
            src = _normalize(v)
            all_time[src] += 1
            if month:
                monthly[month][src] += 1

    return {
        "total_submissions": len(subs),
        "all_time": dict(all_time.most_common()),
        "monthly": {m: dict(c) for m, c in sorted(monthly.items())},
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_cache(db: Session, data: dict) -> None:
    now = datetime.now(timezone.utc)
    row = db.query(DiscoveryCache).filter(DiscoveryCache.key == _CACHE_KEY).first()
    if row:
        row.value      = data
        row.updated_at = now
    else:
        db.add(DiscoveryCache(id=new_uuid(), key=_CACHE_KEY, value=data, updated_at=now))
    db.commit()


# ── Public helper (called from consult_data router) ─────────────────────────

def load_referral_sources(db: Session) -> Optional[dict]:
    """Return cached referral source aggregate, refreshing if stale or missing."""
    if not settings.formstack_token:
        return None

    row = db.query(DiscoveryCache).filter(DiscoveryCache.key == _CACHE_KEY).first()
    if row and row.value:
        data = row.value if isinstance(row.value, dict) else json.loads(row.value)
        synced_at = data.get("synced_at")
        if synced_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(synced_at)
            if age < timedelta(hours=_TTL_HOURS):
                return data

    # Stale or missing — refresh now
    try:
        subs   = _fetch_submissions()
        result = _aggregate(subs)
        _write_cache(db, result)
        return result
    except Exception as exc:
        logger.error(f"Formstack sync failed: {exc}", exc_info=True)
        # Return stale data if available
        if row and row.value:
            return row.value if isinstance(row.value, dict) else json.loads(row.value)
        return None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/formstack/sync")
async def trigger_sync(
    db:   Session = Depends(get_db),
    user: dict    = Depends(auth_required),
):
    """Manually re-sync all Formstack submissions and rebuild the referral cache."""
    if not settings.formstack_token:
        return JSONResponse({"error": "FORMSTACK_TOKEN not configured"}, status_code=503)
    try:
        subs   = _fetch_submissions()
        result = _aggregate(subs)
        _write_cache(db, result)
        return {"ok": True, "total_submissions": result["total_submissions"], "synced_at": result["synced_at"]}
    except Exception as exc:
        logger.error(f"Formstack manual sync failed: {exc}", exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)
