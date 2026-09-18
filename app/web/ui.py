"""Front d'administration.

Une seule page, routee cote client par ancre (#/dashboard, #/pops...). Le
serveur ne rend que le squelette : toutes les vues consomment l'API REST, qui
reste la surface de reference.

Aucune dependance externe, ni framework ni CDN : le controleur doit rester
utilisable sur une VM de management sans acces internet.

LE CACHE DU NAVIGATEUR EST UN PIEGE, PAS UN DETAIL
--------------------------------------------------
``app.js`` et ``app.css`` sont servis en statique et mis en cache par le
navigateur. Apres une mise a jour, un exploitant pouvait donc executer l'ANCIEN
script contre la NOUVELLE API -- et le symptome n'a rien qui oriente vers le
cache : un onglet qui ne montre plus rien. La documentation demandait un
Ctrl+Maj+R, c'est-a-dire qu'elle demandait de se souvenir.

Chaque adresse porte desormais une empreinte du contenu du fichier. Tant que le
fichier ne change pas, l'adresse ne change pas et le cache joue son role ; des
qu'il change, l'adresse change et le navigateur est OBLIGE de recharger.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"], include_in_schema=False)


def _empreinte(nom: str) -> str:
    """Huit caracteres du condensat du fichier, ou son horodatage a defaut.

    Le CONTENU et non la version de l'application : une correction appliquee
    sans changer de version doit quand meme invalider le cache. Un fichier
    illisible ne fait pas echouer la page -- on retombe sur l'heure de
    modification, qui change elle aussi a chaque deploiement.
    """
    chemin = STATIC_DIR / nom
    try:
        return hashlib.sha256(chemin.read_bytes()).hexdigest()[:8]
    except OSError:
        try:
            return str(int(chemin.stat().st_mtime))
        except OSError:
            return "0"


@lru_cache(maxsize=8)
def asset_version(nom: str) -> str:
    """Empreinte mise en cache : le condensat n'est calcule qu'une fois par
    demarrage, pas a chaque affichage de la page."""
    return _empreinte(nom)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"js_version": asset_version("app.js"), "css_version": asset_version("app.css")},
    )
