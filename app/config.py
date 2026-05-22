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

    # Email
    resend_api_key: str = ""
    resend_from_address: str = "alerts@duncanlawonline.com"
    digest_recipient: str = "damonduncan@duncanlawonline.com"

    # App
    app_base_url: str = "http://localhost:8000"
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
