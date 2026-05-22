from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.dependencies import RedirectIfNotAuthenticated

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/reviews", response_class=HTMLResponse)
def reviews(request: Request, user: dict = Depends(auth_required)):
    return templates.TemplateResponse("reviews.html", {
        "request": request,
        "user": user,
        "active_page": "reviews",
    })
