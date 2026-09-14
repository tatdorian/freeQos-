"""Point d'entree FastAPI.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Le cycle de vie ouvre la base, applique le schema, construit les collecteurs et
demarre la boucle de collecte. A l'arret, le scheduler est stoppe puis les
connexions API et base sont fermees proprement.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import admin, antennas_admin, auth, health, metrics, remote, routers_admin, shaping
from app.api.auth import require_identity
from app.config import Settings, get_settings
from app.container import build_container, shutdown_container
from app.logging_conf import setup_logging
from app.web.ui import STATIC_DIR
from app.web.ui import router as ui_router

logger = logging.getLogger(__name__)

DESCRIPTION = """\
Controleur QoS/QoE souverain pour WISP.

**Hors-bande** : cette application n'est jamais sur le chemin des paquets. Elle
tourne sur une VM de management, lit la telemetrie des routeurs MikroTik et la
capacite des liens radio, et alimente une base TimescaleDB.

Elle porte la **boucle centrale lente** (collecte, politique, plans, baselines).
La **boucle locale rapide** qui reagit aux fades radio vit sur le PoP et n'est
pas implementee ici.

**Ecriture (enforcement) active et tracee** : le controleur peut poser et ajuster
des files ``/queue/simple`` sur les routeurs. Toute ecriture est gouvernee par le
drapeau ``ENFORCEMENT_ENABLED`` (lecture seule tant qu'il est faux), ne touche que
les files marquees ``freeqos:managed``, et est journalisee dans
``enforcement_audit`` avec son auteur.

**Authentification requise** : aucun endpoint de l'API n'est accessible sans
identite (session par cookie, ou cle d'API pour les appels machine).
"""

# Swagger UI charge ses assets depuis un CDN et amorce via un script inline :
# la CSP stricte de l'application les bloquerait. On relache donc la CSP pour ces
# seuls chemins de documentation.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
_CSP_APP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "frame-ancestors 'none'; form-action 'self'"
)
_CSP_DOCS = (
    "default-src 'self'; img-src 'self' data: https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Pose les en-tetes de securite sur chaque reponse.

    CSP, anti-clickjacking (X-Frame-Options), anti-sniffing et HSTS : autant de
    garde-fous que l'ancienne configuration (CORS ouvert a tout vent) n'avait pas.
    """

    def __init__(self, app: FastAPI, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if not self._settings.security_headers_enabled:
            return response
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith(_DOCS_PATHS):
            response.headers.setdefault("Content-Security-Policy", _CSP_DOCS)
        else:
            response.headers.setdefault("Content-Security-Policy", _CSP_APP)
        if self._settings.hsts_enabled:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={self._settings.hsts_max_age_s}; includeSubDomains",
            )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    setup_logging(
        settings.log_level,
        json_output=settings.app_env.lower() in {"prod", "production"},
    )
    logger.info("Demarrage de %s %s (env=%s)", settings.app_name, __version__, settings.app_env)

    container = await build_container(settings)
    app.state.container = container

    if settings.scheduler_enabled:
        await container.scheduler.start()
    else:
        logger.warning("Scheduler desactive (SCHEDULER_ENABLED=false) : aucune collecte")

    try:
        yield
    finally:
        logger.info("Arret en cours")
        await shutdown_container(container)
        app.state.container = None


def register_routes(app: FastAPI, settings: Settings) -> None:
    """Monte les routeurs. Extrait de create_app pour que les tests puissent
    monter l'API sur un conteneur factice, sans cycle de vie ni base.

    Tous les routeurs de l'API v1 exigent une identite via ``require_identity`` :
    c'est le point unique qui garantit qu'aucun endpoint n'est joignable
    anonymement. Seuls /health (sonde) et /auth/login (par lequel on s'identifie)
    restent ouverts, ainsi que le squelette HTML de l'interface (qui, lui, ne fait
    qu'appeler l'API deja protegee)."""
    protected = [Depends(require_identity)]

    app.include_router(health.router)
    # Le routeur d'auth porte ses propres verrous (login anonyme, le reste garde).
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(metrics.router, prefix=settings.api_prefix, dependencies=protected)
    app.include_router(admin.router, prefix=settings.api_prefix, dependencies=protected)
    app.include_router(routers_admin.router, prefix=settings.api_prefix, dependencies=protected)
    app.include_router(antennas_admin.router, prefix=settings.api_prefix, dependencies=protected)
    app.include_router(remote.router, prefix=settings.api_prefix, dependencies=protected)
    app.include_router(shaping.router, prefix=settings.api_prefix, dependencies=protected)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(ui_router)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings

    # En-tetes de securite sur toutes les reponses.
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)  # type: ignore[arg-type]

    # CORS : JAMAIS "*". L'interface est servie par la meme origine, donc la liste
    # est vide par defaut ; on n'ouvre qu'a des origines explicitement declarees,
    # et avec ``allow_credentials`` puisque l'auth passe par un cookie.
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["*"],
        )

    register_routes(app, settings)
    return app


app = create_app()
