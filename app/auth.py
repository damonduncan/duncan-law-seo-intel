import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from sqlalchemy.orm import Session
from app.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

_starlette_config = Config(environ={
    "GOOGLE_CLIENT_ID": settings.google_client_id,
    "GOOGLE_CLIENT_SECRET": settings.google_client_secret,
})

oauth = OAuth(_starlette_config)
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@router.get("/auth/login")
async def login(request: Request):
    redirect_uri = settings.google_redirect_uri
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
        raise HTTPException(status_code=400, detail="OAuth authentication failed")

    user_info = token.get("userinfo")
    if not user_info:
        raise HTTPException(status_code=400, detail="Could not retrieve user info")

    email: str = user_info.get("email", "")
    domain = email.split("@")[-1] if "@" in email else ""

    if domain != settings.allowed_email_domain:
        logger.warning(f"Rejected login from unauthorized domain: {email}")
        return RedirectResponse(url="/login?error=unauthorized_domain")

    from app.database import SessionLocal
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.google_sub == user_info["sub"]).first()
        if user:
            user.last_login = datetime.now(timezone.utc)
            user.name = user_info.get("name")
        else:
            user = User(
                email=email,
                name=user_info.get("name"),
                google_sub=user_info["sub"],
                last_login=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
            db.add(user)
        db.commit()
    finally:
        db.close()

    request.session["user"] = {
        "email": email,
        "name": user_info.get("name"),
        "sub": user_info["sub"],
    }

    return RedirectResponse(url="/dashboard")


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


def get_current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")
