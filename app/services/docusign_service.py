"""DocuSign JWT Grant service — fetch attorney-client agreement counts.

Setup required (one-time, in Railway env vars):
  DOCUSIGN_INTEGRATION_KEY  — Integration Key from DocuSign Admin > Apps and Keys
  DOCUSIGN_PRIVATE_KEY      — RSA private key PEM (include header/footer; use \\n for newlines in Railway)

The user ID, account ID, and base URI are hardcoded from the known Duncan Law account.
After adding the RSA public key to the integration key in DocuSign Admin, visit the
consent URL once to grant access:
  https://account.docusign.com/oauth/auth?response_type=code&scope=signature+impersonation
    &client_id={DOCUSIGN_INTEGRATION_KEY}&redirect_uri=https://duncan-law-seo-intel-production.up.railway.app
"""

import time
import calendar as cal_module
import logging

import requests
from authlib.jose import jwt as jose_jwt

logger = logging.getLogger(__name__)

DOCUSIGN_TOKEN_URL = "https://account.docusign.com/oauth/token"
AGREEMENT_SUBJECT  = "PLEASE SIGN: Bankruptcy Attorney-Client Agreement"


def _get_access_token(integration_key: str, user_id: str, private_key_pem: str) -> str:
    """Exchange a signed JWT for a DocuSign access token (JWT Grant flow)."""
    now = int(time.time())
    payload = {
        "iss": integration_key,
        "sub": user_id,
        "aud": "account.docusign.com",
        "iat": now,
        "exp": now + 3600,
        "scope": "signature",
    }

    # Railway env vars often store newlines as literal \n — normalise them.
    key = private_key_pem
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")

    token_bytes = jose_jwt.encode({"alg": "RS256"}, payload, key)
    assertion = token_bytes.decode("utf-8") if isinstance(token_bytes, bytes) else token_bytes

    resp = requests.post(
        DOCUSIGN_TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(
            f"DocuSign token request failed {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()["access_token"]


def fetch_contracts_count(year: int, month: int) -> int:
    """Count completed attorney-client agreements for a given month via DocuSign API.

    Returns 0 (with a warning log) if credentials are not configured.
    """
    from app.config import settings

    if not settings.docusign_integration_key or not settings.docusign_private_key:
        logger.warning(
            "DocuSign credentials not configured (DOCUSIGN_INTEGRATION_KEY / "
            "DOCUSIGN_PRIVATE_KEY). Skipping contract pull."
        )
        return 0

    try:
        access_token = _get_access_token(
            settings.docusign_integration_key,
            settings.docusign_user_id,
            settings.docusign_private_key,
        )
    except Exception as exc:
        logger.error(f"DocuSign JWT auth failed: {exc}")
        return 0

    last_day = cal_module.monthrange(year, month)[1]
    base     = f"{settings.docusign_base_uri}/restapi/v2.1/accounts/{settings.docusign_account_id}"
    headers  = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    count     = 0
    start_pos = 0
    page_size = 200

    while True:
        params = {
            "status":         "completed",
            "from_date":      f"{year}-{month:02d}-01",
            "to_date":        f"{year}-{month:02d}-{last_day}",
            "from_to_status": "Completed",
            "search_text":    AGREEMENT_SUBJECT,
            "count":          page_size,
            "start_position": start_pos,
        }
        try:
            resp = requests.get(f"{base}/envelopes", headers=headers, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error(f"DocuSign envelopes request failed: {exc}")
            break

        for env in data.get("envelopes", []):
            if AGREEMENT_SUBJECT in env.get("emailSubject", ""):
                count += 1

        result_size = int(data.get("resultSetSize", 0))
        total_size  = int(data.get("totalSetSize",  0))
        if start_pos + result_size >= total_size:
            break
        start_pos += result_size

    logger.info(f"DocuSign contracts {year}-{month:02d}: {count}")
    return count
