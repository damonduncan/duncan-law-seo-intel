from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.alerts import Alert

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    alerts = db.query(Alert).order_by(Alert.triggered_at.desc()).limit(100).all()
    return templates.TemplateResponse("alerts.html", {
        "request": request,
        "user": user,
        "alerts": alerts,
        "active_page": "alerts",
    })


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: str,
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if alert and not alert.acknowledged_at:
        alert.acknowledged_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/alerts", status_code=303)


@router.post("/alerts/dismiss-all")
def dismiss_all_alerts(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    db.query(Alert).filter(Alert.acknowledged_at == None).update({"acknowledged_at": now})
    db.commit()
    return RedirectResponse(url="/alerts", status_code=303)
