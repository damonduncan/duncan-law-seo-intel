from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor
from app.models.alerts import Alert, JobRun

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    competitors = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).all()
    own_firm = db.query(Competitor).filter(Competitor.is_own_firm == True).first()
    unacked_alerts = db.query(Alert).filter(Alert.acknowledged_at == None).count()
    last_job = db.query(JobRun).order_by(JobRun.started_at.desc()).first()

    return templates.TemplateResponse("overview.html", {
        "request": request,
        "user": user,
        "own_firm": own_firm,
        "competitors": competitors,
        "unacked_alerts": unacked_alerts,
        "last_job": last_job,
        "active_page": "dashboard",
    })
