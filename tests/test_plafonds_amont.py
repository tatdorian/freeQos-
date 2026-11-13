"""Plafonds EN AMONT : une file par client aussi sur le routeur du haut.

DEMANDE : « un stream venu de l'exterieur plombait le trafic de tout le monde :
il saturait le coeur alors que la queue n'etait qu'en dessous ». Le routeur le
plus haut (role core ou gateway) recoit donc, pour chaque client des NAS, une
file legere a son forfait x facteur : l'exces est coupe avant le coeur.
"""

from __future__ import annotations

from typing import Any

from app.config import RouterConfig, RouterRole, Settings
from app.enforcement.planner import SubscriberTarget
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient


def _service(settings: Settings) -> tuple[ShapingService, FakeRouterOsClient]:
    nas = FakeRouterOsClient()
    nas.add_session("dupont", address="10.20.0.10")
    coeur = FakeRouterOsClient()
    settings.routers = [
        settings.routers[0],
        RouterConfig(
            name="core-test",
            host="192.0.2.1",
            username="qos-ro",
            password="secret-de-lab",
            role=RouterRole.CORE,
        ),
    ]
    clients = {"pop-test": nas, "core-test": coeur}
    registry = RouterRegistry(settings, client_factory=lambda config: clients[config.name])
    service = ShapingService(settings, registry=registry)

    async def cibles(router_name: str) -> tuple[list[Any], list[SubscriberTarget]]:
        # Ce que le NAS dit de ses clients (sessions + forfaits), sans base.
        if router_name != "pop-test":
            return [], []
        return [], [
            SubscriberTarget(
                login="dupont",
                interface="<pppoe-dupont>",
                address="10.20.0.10",
                plan_down_mbps=100,
                plan_up_mbps=20,
                parent="freeqos-parent-bh",
            )
        ]

    service.build_targets = cibles  # type: ignore[method-assign]
    return service, coeur


async def test_le_coeur_porte_la_meme_file_que_le_pop(settings: Settings) -> None:
    """DEMANDE : « la meme queue que pour le pop », pas de +10 %. Meme debit,
    meme CAKE, sur l'adresse du client."""
    service, _coeur = _service(settings)
    await service.registry.reload()

    plan = await service.plan_router("core-test")

    files = [a for a in plan.actions if a.path == "/queue/simple"]
    assert len(files) == 1
    champs = files[0].fields
    # Sur l'ADRESSE du client : l'interface PPPoE n'existe pas sur le coeur.
    assert champs["target"].startswith("10.20.0.10")
    assert champs["parent"] == "none"
    assert "upstream" in champs["comment"]
    # Le meme type que sur le PoP : CAKE, cree sur le coeur s'il manque.
    assert champs["queue"] == "freeqos-cake-up/freeqos-cake-down"
    assert {a.name for a in plan.actions if a.path == "/queue/type"} == {
        "freeqos-cake-up",
        "freeqos-cake-down",
    }
    # Le meme debit que le forfait (100/20 Mbps), sans marge.
    assert champs["max-limit"] == "20000000/100000000"


async def test_un_facteur_reste_possible(settings: Settings) -> None:
    settings.upstream_cap_factor = 1.1
    service, _coeur = _service(settings)
    await service.registry.reload()
    plan = await service.plan_router("core-test")
    files = [a for a in plan.actions if a.path == "/queue/simple"]
    assert files[0].fields["max-limit"] == "22000000/110000000"


def test_actif_par_defaut_et_sans_marge() -> None:
    defaut = Settings(_env_file=None, database_url="postgresql://x:x@h/x")
    assert defaut.upstream_caps is True
    assert defaut.upstream_cap_factor == 1.0


async def test_coupe_le_coeur_ne_recoit_rien(settings: Settings) -> None:
    settings.upstream_caps = False
    service, _coeur = _service(settings)
    await service.registry.reload()
    plan = await service.plan_router("core-test")
    assert not [a for a in plan.actions if a.path == "/queue/simple" and a.verb == "add"]
