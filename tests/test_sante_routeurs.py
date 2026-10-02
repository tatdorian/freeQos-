"""Sante des routeurs : ce que l'equipement dit de lui-meme.

POURQUOI CET ECRAN EXISTE
-------------------------
Deux causes tres banales de "mon abonne n'est pas bride" n'avaient aucune place
dans l'interface : un routeur a 95 % de CPU, qui retarde ou refuse les commandes
d'API, et un routeur qui vient de redemarrer, donc qui a perdu ses files. Les
deux se voient en une commande de lecture, et les chercher ailleurs coute une
demi-heure.

Le tableau rend aussi les routeurs INJOIGNABLES, avec leur motif : une liste
courte qui ne dit pas qui manque est exactement ce qu'on cherche a eviter.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.mikrotik import MikrotikCollector
from app.config import Settings
from app.main import register_routes
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container


@pytest.fixture
def api(settings: Settings, fake_client: FakeRouterOsClient):
    container = build_container(settings, client=fake_client)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), fake_client


async def test_le_collecteur_lit_la_sante_en_une_commande(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    sante = await collector.health()

    assert sante["reachable"] is True
    assert sante["board_name"] == "CHR"
    assert sante["cpu_load_pct"] == 3
    # 1w2d03:04:05 : l'uptime est rendu en secondes, pas en texte a reparser.
    assert sante["uptime_s"] == 788645
    assert sante["version"].startswith("7.")


async def test_la_part_de_memoire_utilisee_est_calculee_une_seule_fois(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    """Le rapport est calcule ici, pas dans l'interface : deux implementations
    du meme pourcentage divergent toujours."""
    fake_client.system_resource = lambda: {  # type: ignore[method-assign]
        "version": "7.21.5",
        "board-name": "CCR2004",
        "uptime": "03:00:00",
        "cpu-load": "42",
        "free-memory": "268435456",
        "total-memory": "1073741824",
    }

    sante = await collector.health()

    assert sante["memory_used_pct"] == 75.0


async def test_sans_memoire_totale_le_pourcentage_reste_inconnu(
    collector: MikrotikCollector, fake_client: FakeRouterOsClient
) -> None:
    """Un pourcentage sans denominateur serait invente. Le double de test ne
    rend pas total-memory : c'est exactement le cas a ne pas remplir."""
    sante = await collector.health()
    assert sante["memory_used_pct"] is None
    assert sante["free_memory"] == 201326592


def test_api_rend_la_sante_de_chaque_routeur(api) -> None:
    client, _ = api

    corps = client.get("/api/v1/pops/health").json()

    assert corps["unreachable"] == 0
    routeur = corps["routers"][0]
    assert routeur["router"] == "pop-test"
    assert routeur["pop_name"] == "PoP Test"
    assert routeur["cpu_load_pct"] == 3


def test_api_un_routeur_muet_figure_avec_son_motif(api) -> None:
    """L'omettre le ferait passer pour un routeur sain -- la pire lecture
    possible sur un ecran de sante."""
    client, fake = api

    def muet() -> dict:
        raise RuntimeError("timeout")

    fake.system_resource = muet  # type: ignore[method-assign]

    corps = client.get("/api/v1/pops/health").json()

    assert corps["unreachable"] == 1
    routeur = corps["routers"][0]
    assert routeur["reachable"] is False
    assert "timeout" in routeur["error"]
    assert routeur["pop_name"] == "PoP Test"


def test_api_la_sante_est_en_lecture_seule(api) -> None:
    client, _ = api
    assert set(client.app.openapi()["paths"]["/api/v1/pops/health"]) == {"get"}
