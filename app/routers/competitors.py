from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/competitors", response_class=HTMLResponse)
def competitors_list(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
):
    competitors = db.query(Competitor).filter(
        Competitor.active == True, Competitor.is_own_firm == False
    ).order_by(Competitor.name).all()

    return templates.TemplateResponse("competitors.html", {
        "request": request,
        "user": user,
        "competitors": competitors,
        "active_page": "competitors",
    })
