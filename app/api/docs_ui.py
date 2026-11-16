"""La documentation interactive de l'API, reservee aux personnes connectees.

``/openapi.json`` decrit CHAQUE route, chaque parametre, chaque champ : c'est
la carte complete de la surface d'attaque. FastAPI la publie par defaut a qui
la demande ; ici elle exige une session (ou une cle d'API), comme le reste de
l'API d'exploitation.

- ``/openapi.json`` : 401 sans connexion ;
- ``/docs`` (Swagger) : renvoie vers la page de connexion sans connexion ;
- ``/redoc`` : n'existe plus (``/docs`` et ``/api-guide`` suffisent).

Le guide ``/api-guide`` reste une page statique ouverte : il ne charge la
reference qu'avec la session du navigateur.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.api.accounts import current_user, optional_user
from app.api.deps import ContainerDep

router = APIRouter(include_in_schema=False)


@router.get("/openapi.json")
async def openapi_schema(
    request: Request, _user: dict[str, Any] = Depends(current_user)
) -> JSONResponse:
    return JSONResponse(request.app.openapi())


@router.get("/docs", response_model=None)
async def swagger(request: Request, container: ContainerDep) -> HTMLResponse | RedirectResponse:
    if await optional_user(request, container) is None:
        return RedirectResponse("/", status_code=303)
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{request.app.title} - API",
        oauth2_redirect_url=None,
    )
