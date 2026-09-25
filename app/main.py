"""Point d'entree FastAPI.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Le cycle de vie ouvre la base, applique le schema, construit les collecteurs et
demarre la boucle de collecte. A l'arret, le scheduler est stoppe puis les
connexions API et base sont fermees proprement.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    accounts,
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
Sovereign QoS/QoE controller for WISPs.

**Out-of-band**: this application is never on the packet path. It runs on a
management VM, reads telemetry from MikroTik routers and the capacity of radio
links, and feeds a TimescaleDB database.

It carries the **slow central loop** (collection, policy, plans, baselines). The
**fast local loop** that reacts to radio fades lives on the PoP and is not
implemented here.

**Writing (enforcement) is active and traced**: the controller can write and
adjust ``/queue/simple`` queues on the routers. Every write is governed by the
``ENFORCEMENT_ENABLED`` flag (read-only while it is false), only touches queues
marked ``freeqos:managed``, and is logged in ``enforcement_audit`` with its
author.

**Two kinds of subscriber**, told apart by the ``kind`` field and then handled by
the same planning path:

- ``pppoe``  : discovered in ``/ppp/active``, address given by the current session;
- ``static`` : static-IP client, **declared by hand** in ``/static-clients``. No
  automatic source exists for it (neither a session nor a RADIUS attribute): the
  inventory entered by the operator is the only truth, and its rate is read from
  the counters of the queue that targets it. Declaring such a client WRITES its
  queue straight away, and the response says what was written -- or what
  prevents it (``enforcement``). ``GET /static-clients/enforcement`` returns the
  same state for the whole inventory.

**Assisted detection, never automatic**: a census of the PoP (``/pops/census``)
cross-checks seven sources of presence -- ``/ip/arp``, DHCP leases, PPPoE
sessions, bridge table, static routes, queues already written, neighbours -- over
the subnets the router really serves (``/ip/address``), and not over interface
names alone. It serves two purposes only: confirming the presence of an already
declared client, and PROPOSING candidates in ``/static-clients/candidates``. A
candidate is not a client: a printer or another operator device leaves the same
trace. No candidate is ever shaped, none receives a plan, and there is
deliberately no route to promote one -- declaring goes through
``POST /static-clients`` with a subscribed rate only a human knows.

**Closed QoE loop**: a periodic job reads the composite QoE score (bufferbloat +
idle latency) and tightens the SHARED envelope of a sector that is dropping off
-- never the subscribed plan of a subscriber. Same safeguards as the other
automatic loops: subject to ``ENFORCEMENT_ENABLED``, never a purge, and the plan
goes through the same planner, so it stays diffable and auditable.

**Where this controller sits**: upstream of the core, just behind the internet
egress, and at the PoP -- at both ends of the network, never in the middle. The
core exports nothing and is not polled: measurement adds no load to it, in
either direction. That is the point of exported NetFlow (a few tens of kbit/s)
over a port mirror, which would copy every byte onto the collection link in both
directions.

**Traffic (NetFlow v5 / v9 / IPFIX)**: the controller LISTENS to exported flows
and derives timestamped volume from them, per subscriber and per usage. Since the
same byte is seen at both measurement points, each one is recorded WITH its
vantage and usage is read from a single one (``NETFLOW_ACCOUNTING_VANTAGE``).
What the flows show that matches no record goes into an entry-aid list -- never
into the inventory.

**Who connects to what (ipfinder)**: for every flow matched to a subscriber, the
REMOTE address is kept, then NAMED -- an embedded catalogue of published prefixes
(Netflix, YouTube, Twitch, the CDNs...), reverse name (PTR), and registry (RDAP,
off by default). No content inspection: the traffic is encrypted, and it stays
that way. An address never seen before enters the queue the moment a client
reaches it and is named on the next pass: discovery is DYNAMIC, nothing is to be
declared. ``GET /netflow/connections`` shows the CURRENT window (the only live
view), ``/netflow/destinations`` what is reached over the period, and
``/netflow/destinations/{ip}`` the full record of one address.

**Traffic restrictions** (``/traffic-rules``): block or cap traffic designated by
a SERVICE or a CATEGORY ("netflix", "streaming"), for every client or for some.
A rule is not a frozen list of addresses: its set is recomputed at every
reconciliation from the catalogue AND from what NetFlow discovered, so a new
server joins the list on the router by itself. Writing creates an
``/ip/firewall/address-list`` and, depending on the action, ``filter`` rules
(reject) or ``mangle`` + ``queue tree`` (cap). It goes through the SAME path as
the queues: ``ENFORCEMENT_ENABLED``, a viewable plan, an audit trail in
``enforcement_audit``, and nothing that does not carry ``freeqos:managed`` is
touched.

**Public API, Preseem-compatible contract** (``/model/v1`` and ``/usage/v1``, key
in Basic authentication): five collections -- ``accounts``, ``packages``,
``sites``, ``access_points``, ``services`` -- with ``GET``, idempotent
``PUT /{id}`` and ``DELETE``. A billing system that talked to Preseem changes the
base URL and the key, nothing else. A service written through the API lands in
the SAME inventory as manual entry, marked ``source='api'``, and the API never
overwrites a record entered by hand (409).
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
    # Connexion et comptes : ces routes portent leurs propres gardes (ouvrir une
    # session ne peut pas exiger d'en avoir une).
    app.include_router(accounts.router, prefix=settings.api_prefix)
    # TOUTE route d'exploitation exige une session, et un compte en lecture
    # seule n'y obtient que les methodes de lecture. La garde est posee ICI,
    # une fois, plutot que route par route : une route ajoutee demain est
    # protegee sans qu'on ait a y penser.
    proteges = [Depends(accounts.require_access)]
    for module in (
        metrics,
        admin,
        routers_admin,
        antennas_admin,
        capacity,
        shaping,
        static_clients,
        pop_census,
        settings_api,
        netflow,
        traffic_rules,
        api_keys,
    ):
        app.include_router(module.router, prefix=settings.api_prefix, dependencies=proteges)
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

    @app.middleware("http")
    async def chronometre(request: Request, call_next: Any) -> Any:
        """Chaque reponse dit ce qu'elle a coute (``Server-Timing``, visible dans
        l'onglet Reseau du navigateur), et une requete de plus d'une seconde est
        journalisee avec sa route : une page lente se diagnostique sans outil."""
        debut = time.perf_counter()
        response = await call_next(request)
        duree_ms = (time.perf_counter() - debut) * 1000
        response.headers["Server-Timing"] = f"app;dur={duree_ms:.1f}"
        if duree_ms > 1000 and request.url.path.startswith(settings.api_prefix):
            logger.warning(
                "Requete lente : %s %s en %.0f ms", request.method, request.url.path, duree_ms
            )
        return response

    register_routes(app, settings)
    return app


app = create_app()
