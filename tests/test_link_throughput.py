"""Debit des liens : compteurs de ports -> debits -> exposition.

Le point sensible n'est pas le calcul (il est partage avec les abonnes) mais
l'ATTRIBUTION : RouterOS compte par interface, pas par adjacence. Ces tests
verifient qu'on ne fait pas passer un debit de port pour un debit de lien quand
plusieurs voisins se partagent le port, et qu'on n'ecrit jamais un debit tire
d'une seule lecture.
"""

from __future__ import annotations

import pytest

from app.collectors.mikrotik import MikrotikCollector, is_physical_interface, parse_flag
from app.collectors.radius import MockPlanProvider
from app.collectors.uisp import MockBackhaulProvider
from app.config import RouterConfig, Settings
from app.db.directory import InMemoryDirectory
from app.db.writer import InMemoryMetricsWriter
from app.services.collection import JOB_LINKS, CollectionService
from tests.conftest import FakeRouterOsClient


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_service(
    settings: Settings, clients: dict[str, FakeRouterOsClient], clock: Clock
) -> tuple[CollectionService, InMemoryMetricsWriter]:
    collectors = [MikrotikCollector(cfg, client=clients[cfg.name]) for cfg in settings.routers]
    writer = InMemoryMetricsWriter()
    service = CollectionService(
        settings,
        collectors=collectors,
        backhaul_provider=MockBackhaulProvider(clock=clock),
        plan_provider=MockPlanProvider(),
        directory=InMemoryDirectory(),
        writer=writer,
        backhauls=[],
        clock=clock,
    )
    return service, writer


# --------------------------------------------------------------- filtrage


def test_les_interfaces_de_session_sont_ecartees() -> None:
    """Une interface <pppoe-x> est deja suivie abonne par abonne : la reprendre
    ici dupliquerait chaque serie dans la table des liens."""
    assert is_physical_interface({"name": "ether1", "type": "ether"})
    assert is_physical_interface({"name": "bridge-lan", "type": "bridge"})
    assert is_physical_interface({"name": "vlan100", "type": "vlan"})
    assert not is_physical_interface({"name": "<pppoe-alice>", "type": "pppoe-in"})
    assert not is_physical_interface({"name": "pppoe-client", "type": "pppoe-out"})
    assert not is_physical_interface({"name": "", "type": "ether"})


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [("true", True), ("false", False), ("yes", True), (True, True), ("", None), (None, None)],
)
def test_lecture_des_booleens_routeros(brut: object, attendu: bool | None) -> None:
    assert parse_flag(brut) is attendu


# ---------------------------------------------------------------- collecte


async def test_le_collecteur_lit_les_ports_et_leur_capacite(
    router_config: RouterConfig,
) -> None:
    client = FakeRouterOsClient()
    client.add_interface("ether1", rx_byte=1_000, tx_byte=2_000, speed="10Gbps")
    client.add_interface("ether2", rx_byte=10, tx_byte=20, speed="1Gbps")
    client.add_session("alice", rx_byte=5, tx_byte=9)  # ne doit pas apparaitre

    echantillons = await MikrotikCollector(router_config, client=client).collect_interfaces()

    noms = {e.interface for e in echantillons}
    assert noms == {"ether1", "ether2"}
    par_nom = {e.interface: e for e in echantillons}
    assert par_nom["ether1"].capacity_mbps == 10_000
    assert par_nom["ether1"].rx_bytes == 1_000
    assert par_nom["ether1"].running is True
    # Le debit n'est PAS calcule par le collecteur : il faut deux lectures.
    assert par_nom["ether1"].rx_bps is None


async def test_un_seul_passage_ne_produit_aucun_debit(settings: Settings) -> None:
    """Meme garde-fou que pour les abonnes : un compteur cumulatif seul ne dit
    rien du debit. Mieux vaut un trou qu'une valeur inventee."""
    client = FakeRouterOsClient()
    client.add_interface("ether1", rx_byte=0, tx_byte=0)
    clock = Clock()
    service, writer = build_service(settings, {"pop-test": client}, clock)

    resultat = await service.collect_links()

    assert resultat.job == JOB_LINKS
    assert resultat.ok
    assert len(writer.interface_rows) == 1
    assert writer.interface_rows[0].rx_bps is None
    assert writer.interface_rows[0].tx_bps is None


async def test_deux_passages_donnent_le_debit_du_port(settings: Settings) -> None:
    client = FakeRouterOsClient()
    client.add_interface("ether1", rx_byte=0, tx_byte=0, speed="1Gbps")
    clock = Clock()
    service, writer = build_service(settings, {"pop-test": client}, clock)

    await service.collect_links()
    # 10 s plus tard : 125 Mo emis vers le voisin, 12,5 Mo recus.
    clock.advance(10.0)
    client.advance_interface("ether1", rx_delta=12_500_000, tx_delta=125_000_000)
    await service.collect_links()

    dernier = writer.interface_rows[-1]
    assert dernier.interface == "ether1"
    assert dernier.tx_bps == pytest.approx(100_000_000)  # 100 Mbps descendant
    assert dernier.rx_bps == pytest.approx(10_000_000)  # 10 Mbps montant
    assert dernier.capacity_mbps == 1_000


async def test_un_port_qui_disparait_oublie_son_point_de_reference(
    settings: Settings,
) -> None:
    """Sinon, le jour ou le meme nom reapparait, l'ecart de compteurs sur
    plusieurs heures produirait un debit absurde."""
    client = FakeRouterOsClient()
    client.add_interface("ether1", rx_byte=0, tx_byte=0)
    clock = Clock()
    service, _ = build_service(settings, {"pop-test": client}, clock)

    await service.collect_links()
    assert len(service.interface_rates) == 1

    client.interfaces_rows.clear()
    clock.advance(10.0)
    await service.collect_links()
    assert len(service.interface_rates) == 0


async def test_un_routeur_injoignable_ne_bloque_pas_les_autres(
    settings: Settings, router_config: RouterConfig
) -> None:
    muet = FakeRouterOsClient()
    muet.add_interface("ether1")
    muet.raise_on_ppp = TimeoutError("routeur muet")

    def interfaces_ko() -> list[dict[str, object]]:
        raise TimeoutError("routeur muet")

    muet.interfaces = interfaces_ko  # type: ignore[method-assign]

    vivant = FakeRouterOsClient()
    vivant.add_interface("ether1", rx_byte=0, tx_byte=0)

    routeurs = [
        RouterConfig(name="pop-ko", host="192.0.2.99", password="x"),
        RouterConfig(name="pop-ok", host="192.0.2.98", password="x"),
    ]
    reglages = settings.model_copy(update={"routers": routeurs})
    service, writer = build_service(reglages, {"pop-ko": muet, "pop-ok": vivant}, Clock())

    resultat = await service.collect_links()

    assert not resultat.ok
    assert any("pop-ko" in e for e in resultat.errors)
    # Le PoP joignable a quand meme ete ecrit.
    assert [r.router_name for r in writer.interface_rows] == ["pop-ok"]


# ------------------------------------------------------- mesure instantanee


async def test_mesure_instantanee_via_monitor_traffic(router_config: RouterConfig) -> None:
    """Repond a "combien passe MAINTENANT" sans attendre deux cycles."""
    client = FakeRouterOsClient()
    client.monitor_rates["ether2"] = (12_000_000, 340_000_000)
    collecteur = MikrotikCollector(router_config, client=client)

    mesure = await collecteur.measure_interface("ether2")

    assert mesure["interface"] == "ether2"
    assert mesure["rx_bps"] == 12_000_000
    assert mesure["tx_bps"] == 340_000_000


async def test_mesure_instantanee_sur_routeur_inconnu(settings: Settings) -> None:
    client = FakeRouterOsClient()
    service, _ = build_service(settings, {"pop-test": client}, Clock())
    with pytest.raises(KeyError):
        await service.measure_link("pop-absent", "ether1")
