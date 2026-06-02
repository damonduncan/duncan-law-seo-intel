import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting Competitor Analysis & SEO Intelligence Tool")
    settings.check_pacer_expiry()

    # Sync competitors config to database
    if settings.database_url:
        try:
            from app.database import SessionLocal
            from app.services.config_loader import sync_competitors
            db = SessionLocal()
            try:
                sync_competitors(db)
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Config sync failed on startup: {e}", exc_info=True)

    # Start scheduler
    from app.scheduler import scheduler, register_jobs
    register_jobs()
    scheduler.start()
    logger.info("Scheduler started")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(
    title="Competitor Analysis & SEO Intelligence",
    description="Local SEO and PACER filing intelligence for Duncan Law",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=86400 * 7,  # 7 days
    https_only=not settings.is_development,
    same_site="lax",
)

# Auth routes
from app.auth import router as auth_router
app.include_router(auth_router)

# Page routes
from app.routers.overview import router as overview_router
from app.routers.rankings import router as rankings_router
from app.routers.reviews import router as reviews_router
from app.routers.filings import router as filings_router
from app.routers.competitors import router as competitors_router
from app.routers.alerts import router as alerts_router
from app.routers.admin import router as admin_router
from app.routers.briefing import router as briefing_router
from app.routers.ai_chat import router as ai_chat_router

app.include_router(overview_router)
app.include_router(rankings_router)
app.include_router(reviews_router)
app.include_router(filings_router)
app.include_router(competitors_router)
app.include_router(alerts_router)
app.include_router(admin_router)
app.include_router(briefing_router)
app.include_router(ai_chat_router)

templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    user = request.session.get("user")
    if user:
        return RedirectResponse(url="/dashboard")
    return RedirectResponse(url="/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
    })


@app.exception_handler(307)
async def redirect_handler(request: Request, exc):
    return RedirectResponse(url=exc.headers["Location"])
