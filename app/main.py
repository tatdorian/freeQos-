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
    api_keys,
    capacity,
    health,
    metrics,
    model_v1,
    netflow,
    pop_census,
    routers_admin,
    shaping,
    static_clients,
    traffic_rules,
    usage_v1,
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
  la file qui le vise. Declarer un tel client POSE sa file dans la foulee, et la
  reponse dit ce qui a ete ecrit -- ou ce qui l'en empeche (``enforcement``).
  ``GET /static-clients/enforcement`` rend le meme etat pour tout l'inventaire.

**Detection assistee, jamais automatique** : un recensement du PoP
(``/pops/census``) croise sept sources de presence -- ``/ip/arp``, baux DHCP,
sessions PPPoE, table de ponts, routes statiques, files deja posees, voisinage --
sur les sous-reseaux que le routeur dessert reellement (``/ip/address``), et non
sur le seul nom des interfaces. Il sert a deux choses seulement : confirmer la
presence d'un client deja declare, et PROPOSER des candidats dans
``/static-clients/candidates``. Un candidat n'est pas un client : une imprimante
ou l'equipement d'un autre operateur laissent la meme trace. Aucun candidat
n'est jamais faconne, aucun ne recoit de plan, et il n'existe deliberement
aucune route pour le promouvoir -- declarer passe par
``POST /static-clients`` avec un debit souscrit que seul un humain connait.

**Boucle fermee QoE** : un job periodique lit le score de QoE composite
(bufferbloat + latence a vide) et resserre l'enveloppe PARTAGEE d'un secteur qui
decroche -- jamais le plan souscrit d'un abonne. Meme garde-fous que les autres
boucles automatiques : soumise a ``ENFORCEMENT_ENABLED``, jamais de purge, et le
plan passe par le meme planificateur, donc reste diffable et auditable.

**Ou ce controleur se place** : en amont du coeur, juste derriere la sortie
internet, et au niveau du PoP -- aux deux extremites du reseau, jamais au milieu.
Le coeur n'exporte rien et n'est pas interroge : la mesure ne lui ajoute aucune
charge, ni a l'aller ni au retour. C'est l'interet du flux NetFlow exporte
(quelques dizaines de kbit/s) sur un miroir de port, qui recopierait chaque octet
sur le lien de collecte dans les deux sens.

**Trafic (NetFlow v5 / v9 / IPFIX)** : le controleur ECOUTE les flux exportes et
en tire du volume date, par abonne et par usage. Le meme octet etant vu aux deux
points de mesure, chacun est enregistre AVEC sa mesure et la consommation se lit
depuis un seul (``NETFLOW_ACCOUNTING_VANTAGE``). Ce que les flux montrent et qui
n'est rattache a aucune fiche va dans une liste d'aide a la saisie -- jamais dans
l'inventaire.

**Qui se connecte a quoi (ipfinder)** : pour chaque flux rattache a un abonne,
l'adresse DISTANTE est retenue, puis NOMMEE -- catalogue de blocs publies embarque
(Netflix, YouTube, Twitch, les CDN...), nom inverse (PTR), et registre (RDAP,
coupe par defaut). Aucune inspection de contenu : le trafic est chiffre, il le
reste. Une adresse jamais vue entre en file d'attente au moment ou un client
l'atteint et est nommee au passage suivant : la decouverte est DYNAMIQUE, rien
n'est a declarer. ``GET /netflow/connections`` montre la fenetre EN COURS (la
seule vue en direct), ``/netflow/destinations`` ce qui est atteint sur la
periode, et ``/netflow/destinations/{ip}`` la fiche complete d'une adresse.

**Restrictions de trafic** (``/traffic-rules``) : bloquer ou plafonner un trafic
designe par un SERVICE ou une FAMILLE ("netflix", "streaming"), pour tous les
clients ou pour certains. Une regle n'est pas une liste d'adresses figee : son
ensemble est recalcule a chaque reconciliation depuis le catalogue ET depuis ce
que NetFlow a decouvert, donc un serveur nouveau rejoint la liste posee sur le
routeur tout seul. L'ecriture pose une ``/ip/firewall/address-list`` et, selon
l'action, des regles ``filter`` (rejet) ou ``mangle`` + ``queue tree`` (plafond).
Elle passe par le MEME chemin que les files : ``ENFORCEMENT_ENABLED``, plan
affichable, audit dans ``enforcement_audit``, et rien qui ne porte pas
``freeqos:managed`` n'est touche.

**API publique, contrat compatible Preseem** (``/model/v1`` et ``/usage/v1``,
cle en authentification Basic) : cinq collections -- ``accounts``, ``packages``,
``sites``, ``access_points``, ``services`` -- en ``GET``, ``PUT /{id}`` idempotent
et ``DELETE``. Un systeme de facturation qui parlait a Preseem change l'URL de
base et la cle, rien d'autre. Un service ecrit par l'API atterrit dans le MEME
inventaire que la saisie manuelle, marque ``source='api'``, et l'API n'ecrase
jamais une fiche saisie a la main (409).
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
    app.include_router(capacity.router, prefix=settings.api_prefix)
    app.include_router(shaping.router, prefix=settings.api_prefix)
    app.include_router(static_clients.router, prefix=settings.api_prefix)
    app.include_router(pop_census.router, prefix=settings.api_prefix)
    app.include_router(settings_api.router, prefix=settings.api_prefix)
    app.include_router(netflow.router, prefix=settings.api_prefix)
    app.include_router(traffic_rules.router, prefix=settings.api_prefix)
    app.include_router(api_keys.router, prefix=settings.api_prefix)
    # API PUBLIQUE. Volontairement HORS du prefixe d'exploitation : son chemin
    # est le contrat que les systemes de facturation connaissent deja
    # (/model/v1, /usage/v1), et le deplacer suffirait a casser la
    # compatibilite qui fait tout l'interet de ces routes.
    app.include_router(model_v1.router)
    app.include_router(usage_v1.router)
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
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    register_routes(app, settings)
    return app


app = create_app()
