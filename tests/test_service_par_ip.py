"""Un service pousse par son IP (contrat Preseem) est situe et bride par elle.

Constate au rejeu des appels Preseem : la facturation designe le client par son
IP, pas par un nom de site. Le service partait "sans-routeur" (site inconnu du
controleur) ou "ecarte" (l'IP etait deja celle d'une session PPPoE, et une
seconde file sur la meme cible ne bride rien). Le client n'etait donc pas limite.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.collectors import mikrotik
from app.config import Settings
from app.services.unplaced import ip_by_mac
from tests.conftest import FakeRouterOsClient
from tests.test_pose_immediate import (
    InventaireMemoire,
    RouteurQuiSeSouvient,
    _client_statique,
)
from tests.test_shaping_service import DepotBoosts, make_service


class Metriques:
    def __init__(self, abonnes: list[dict[str, Any]]) -> None:
        self.abonnes = abonnes

    async def subscriber_latest(self, **_: Any) -> list[dict[str, Any]]:
        return self.abonnes

    async def backhaul_latest(self, **_: Any) -> list[dict[str, Any]]:
        return []

    async def subscriber_at_address(self, address: str) -> dict[str, Any] | None:
        for a in self.abonnes:
            if a.get("last_ip") == address and a.get("kind") != "static":
                return a
        return None


@pytest.fixture(autouse=True)
def _reseaux() -> Any:
    mikrotik._RESEAUX_CONNECTES.clear()
    yield
    mikrotik._RESEAUX_CONNECTES.clear()


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    return FakeRouterOsClient()


@pytest.fixture
def cadre(settings: Settings) -> Settings:
    settings.routers[0].pop_name = "Alpha"
    settings.enforcement_enabled = True
    return settings


def _service(settings: Settings, routeur: FakeRouterOsClient, clients, abonnes) -> Any:
    ecriture = RouteurQuiSeSouvient(routeur)
    service = make_service(
        settings,
        routeur,
        repository=DepotBoosts(),
        metrics=Metriques(abonnes),
        static_clients=InventaireMemoire(clients),
        write_client_factory=lambda config: ecriture,
    )
    return service, ecriture


def test_le_routeur_qui_porte_le_reseau_est_trouve_prefixe_le_plus_long() -> None:
    mikrotik.remember_addresses("core", [{"address": "10.0.0.1/16", "interface": "bridge"}])
    mikrotik.remember_addresses(
        "nas-a",
        [
            {"address": "10.0.20.1/24", "interface": "vlan120"},
            {"address": "10.255.0.10/32", "interface": "lo"},
        ],
    )
    assert mikrotik.router_serving("10.0.20.5") == ("nas-a", "vlan120")
    assert mikrotik.router_serving("10.0.99.5") == ("core", "bridge")
    assert mikrotik.router_serving("8.8.8.8") is None


async def test_une_ip_fixe_est_placee_sur_le_routeur_qui_porte_son_reseau(
    cadre: Settings, routeur: FakeRouterOsClient
) -> None:
    """Le site pousse ne correspond a aucun PoP : l'IP, elle, dit ou il est."""
    nom = cadre.routers[0].name
    mikrotik.remember_addresses(nom, [{"address": "10.20.0.1/24", "interface": "vlan120"}])
    client = _client_statique(
        reference="svc-fixe",
        pop_name="non-affecte",
        address="10.20.0.5/32",
        plan_down_mbps=50.0,
        plan_up_mbps=5.0,
    )
    service, ecriture = _service(cadre, routeur, [client], [])
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="svc-fixe", pop_name="non-affecte", author="api:model", address="10.20.0.5/32"
    )

    assert rapport["state"] == "file-posee"
    files = [a for a in ecriture.executed if a.path == "/queue/simple"]
    assert [a.name for a in files] == ["freeqos-svc-fixe"]
    assert files[0].fields["max-limit"] == "5000000/50000000"


async def test_une_ip_de_session_pppoe_donne_son_debit_a_la_session(
    cadre: Settings, routeur: FakeRouterOsClient
) -> None:
    """Pas de seconde file en double : la session prend le debit du service."""
    routeur.add_session("dupont", address="100.64.1.13")
    abonnes = [
        {
            "login": "dupont",
            "kind": "pppoe",
            "pop_name": "Alpha",
            "plan_down_mbps": 300.0,
            "plan_up_mbps": 50.0,
            "last_ip": "100.64.1.13",
        }
    ]
    client = _client_statique(
        reference="svc-dupont",
        pop_name="non-affecte",
        address="100.64.1.13/32",
        plan_down_mbps=100.0,
        plan_up_mbps=20.0,
    )
    service, ecriture = _service(cadre, routeur, [client], abonnes)
    await service.registry.reload()

    rapport = await service.enforce_static_client(
        reference="svc-dupont",
        pop_name="non-affecte",
        author="api:model",
        address="100.64.1.13/32",
    )

    assert rapport["state"] == "file-posee"
    assert rapport["session"] == "dupont"
    files = [a for a in ecriture.executed if a.path == "/queue/simple"]
    assert [a.name for a in files] == ["freeqos-dupont"]
    assert files[0].fields["max-limit"] == "20000000/100000000"


async def test_sans_forfait_la_limite_par_defaut_s_applique_a_la_session(
    cadre: Settings, routeur: FakeRouterOsClient
) -> None:
    routeur.add_session("dupont", address="100.64.1.13")
    abonnes = [{"login": "dupont", "kind": "pppoe", "pop_name": "Alpha", "last_ip": "100.64.1.13"}]
    client = _client_statique(
        reference="svc-dupont",
        address="100.64.1.13/32",
        plan_down_mbps=None,
        plan_up_mbps=None,
    )
    service, _ = _service(cadre, routeur, [client], abonnes)
    await service.registry.reload()

    _liens, cibles = await service.build_targets(cadre.routers[0].name)

    dupont = next(c for c in cibles if c.login == "dupont")
    assert (dupont.plan_down_mbps, dupont.plan_up_mbps) == (100.0, 20.0)
    assert all(c.login != "svc-dupont" for c in cibles)


def test_la_mac_du_cpe_donne_son_ip_par_arp_ou_dhcp() -> None:
    table = ip_by_mac(
        [
            {"address": "10.20.0.5", "mac-address": "aa:bb:cc:00:00:01"},
            {"active-address": "10.20.0.9", "active-mac-address": "AA-BB-CC-00-00-02"},
        ]
    )
    assert table == {"AA:BB:CC:00:00:01": "10.20.0.5", "AA:BB:CC:00:00:02": "10.20.0.9"}
