import os
from datetime import date
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = ""

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/callback"
    allowed_email_domain: str = "duncanlawonline.com"

    # Session
    secret_key: str = "dev-secret-change-in-production"

    # DataForSEO
    dataforseo_login: str = ""
    dataforseo_password: str = ""

    # Google APIs
    google_places_api_key: str = ""
    google_business_profile_credentials: str = ""
    use_business_profile_api: bool = False

    # PACER
    pacer_username: str = ""
    pacer_password: str = ""
    pacer_client_code: str = ""
    pacer_password_expires: Optional[str] = None

    # AI recommendations
    anthropic_api_key: str = ""

    # Email
    resend_api_key: str = ""
    resend_from_address: str = "alerts@duncanlawonline.com"
    digest_recipient: str = "damonduncan@duncanlawonline.com"

    # Google Analytics 4
    ga_property_id: str = "359981496"
    ga_credentials_json: str = ""   # full service account JSON string (GA_CREDENTIALS_JSON env var)

    # Google Calendar (consultation sync)
    calendar_credentials_json: str = ""  # service account JSON with domain-wide delegation (CALENDAR_CREDENTIALS_JSON env var)

    # DocuSign (attorney-client agreement counts)
    # Set DOCUSIGN_INTEGRATION_KEY and DOCUSIGN_PRIVATE_KEY in Railway to enable automated monthly pulls.
    docusign_integration_key: str = ""
    docusign_private_key: str     = ""  # RSA private key PEM — use \\n for newlines in Railway env var
    # Known values for Duncan Law — no need to override these in Railway
    docusign_user_id:    str = "234f1c97-dd04-4c37-b829-cd64cdc7b9b1"
    docusign_account_id: str = "0f3a2e88-0dfd-494d-a4b3-a273c3f74336"
    docusign_base_uri:   str = "https://na3.docusign.net"

    # Briefing
    briefing_token: str = ""   # set BRIEFING_TOKEN env var to share /briefing without OAuth

    # App
    app_base_url: str = "https://duncan-law-seo-intel-production.up.railway.app"
    environment: str = "development"
    log_level: str = "INFO"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    def check_pacer_expiry(self) -> None:
        if not self.pacer_password_expires:
            return
        try:
            expires = date.fromisoformat(self.pacer_password_expires)
            days_left = (expires - date.today()).days
            if days_left <= 30:
                import logging
                logging.getLogger(__name__).warning(
                    f"PACER password expires in {days_left} days ({self.pacer_password_expires}). "
                    "Update PACER_PASSWORD and PACER_PASSWORD_EXPIRES in Railway env vars."
                )
        except ValueError:
            pass


settings = Settings()
