"""Point d'entree FastAPI.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Le cycle de vie ouvre la base, applique le schema, construit les collecteurs et
demarre la boucle de collecte. A l'arret, le scheduler est stoppe puis les
connexions API et base sont fermees proprement.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    admin,
    antennas_admin,
    health,
    metrics,
    remote,
    routers_admin,
    shaping,
    static_clients,
)
from app.api import (
    settings as settings_api,
)
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

**Deux natures d'abonnes**, distinguees par le champ ``kind`` et traitees ensuite par
le meme chemin de planification :

- ``pppoe``  : decouvert dans ``/ppp/active``, adresse donnee par la session en cours ;
- ``static`` : client a IP fixe, **declare a la main** dans ``/static-clients``. Aucune
  source automatique n'existe pour lui (ni session, ni attribut RADIUS) : l'inventaire
  saisi par l'operateur est la seule verite, et son debit se lit sur les compteurs de
  la file qui le vise.

**Detection assistee, jamais automatique** : la table ARP des routeurs
(``/ip/arp``, filtree aux VLAN qui n'hebergent pas de serveur PPPoE) sert a deux
choses seulement -- confirmer la presence d'un client deja declare, et PROPOSER
des candidats dans ``/static-clients/candidates``. Un candidat n'est pas un
client : une imprimante ou l'equipement d'un autre operateur laissent la meme
trace. Aucun candidat n'est jamais faconne, aucun ne recoit de plan, et il
n'existe deliberement aucune route pour le promouvoir -- declarer passe par
``POST /static-clients`` avec un debit souscrit que seul un humain connait.

**Boucle fermee QoE** : un job periodique lit le score de QoE composite
(bufferbloat + latence a vide) et resserre l'enveloppe PARTAGEE d'un secteur qui
decroche -- jamais le plan souscrit d'un abonne. Meme garde-fous que les autres
boucles automatiques : soumise a ``ENFORCEMENT_ENABLED``, jamais de purge, et le
plan passe par le meme planificateur, donc reste diffable et auditable.
"""


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
    monter l'API sur un conteneur factice, sans cycle de vie ni base."""
    app.include_router(health.router)
    app.include_router(metrics.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)
    app.include_router(routers_admin.router, prefix=settings.api_prefix)
    app.include_router(antennas_admin.router, prefix=settings.api_prefix)
    app.include_router(remote.router, prefix=settings.api_prefix)
    app.include_router(shaping.router, prefix=settings.api_prefix)
    app.include_router(static_clients.router, prefix=settings.api_prefix)
    app.include_router(settings_api.router, prefix=settings.api_prefix)
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

    # Le front d'admin est servi par la meme origine ; CORS n'est ouvert que si
    # un outil externe doit consommer l'API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.app_env.lower() == "lab" else [],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    register_routes(app, settings)
    return app


app = create_app()
