"""Front d'admin minimal.

HTML rendu cote serveur + HTMX : pas de chaine de build, pas de dependance
front, et la page se rafraichit toute seule. C'est suffisant pour verifier en
lab que la collecte tourne ; une interface plus riche viendra apres l'API.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.deps import CollectionDep, RepositoryDep, SchedulerDep

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html")


@router.get("/ui/fragments/overview", response_class=HTMLResponse)
async def fragment_overview(
    request: Request,
    repo: RepositoryDep,
    scheduler: SchedulerDep,
    collection: CollectionDep,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_overview.html",
        {
            "counters": await repo.counters(),
            "jobs": scheduler.status(),
            "tracked": len(collection.rates),
            "resets": collection.rates.resets_detected,
        },
    )


@router.get("/ui/fragments/subscribers", response_class=HTMLResponse)
async def fragment_subscribers(request: Request, repo: RepositoryDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_subscribers.html",
        {"rows": await repo.subscriber_latest(limit=25)},
    )


@router.get("/ui/fragments/backhauls", response_class=HTMLResponse)
async def fragment_backhauls(request: Request, repo: RepositoryDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_backhauls.html",
        {"rows": await repo.backhaul_latest()},
    )
