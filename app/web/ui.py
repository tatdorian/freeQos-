"""Front d'administration.

Une seule page, routee cote client par ancre (#/dashboard, #/pops...). Le
serveur ne rend que le squelette : toutes les vues consomment l'API REST, qui
reste la surface de reference.

Aucune dependance externe, ni framework ni CDN : le controleur doit rester
utilisable sur une VM de management sans acces internet.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")
