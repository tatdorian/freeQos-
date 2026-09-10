"""Service de collecte : le chemin complet routeur -> referentiel -> ecriture,
sans PostgreSQL ni equipement reel."""

from __future__ import annotations

from app.collectors.mikrotik import MikrotikCollector
from app.collectors.radius import MockPlanProvider
from app.collectors.uisp import MockBackhaulProvider
from app.config import BackhaulConfig, RouterConfig, Settings
from app.db.directory import InMemoryDirectory
from app.db.writer import InMemoryMetricsWriter
from app.services.collection import CollectionService
from tests.conftest import FakeRouterOsClient


class Clock:
    """Horloge monotone pilotee par le test."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_service(
    settings: Settings,
    clients: dict[str, FakeRouterOsClient],
    *,
    routers: list[RouterConfig] | None = None,
    backhauls: list[BackhaulConfig] | None = None,
    clock: Clock | None = None,
) -> tuple[CollectionService, InMemoryMetricsWriter, InMemoryDirectory]:
    clock = clock or Clock()
    routers = routers if routers is not None else list(settings.routers)
    collectors = [MikrotikCollector(cfg, client=clients[cfg.name]) for cfg in routers]
    writer = InMemoryMetricsWriter()
    directory = InMemoryDirectory()
    service = CollectionService(
        settings,
        collectors=collectors,
        backhaul_provider=MockBackhaulProvider(clock=clock),
        plan_provider=MockPlanProvider(),
        directory=directory,
        writer=writer,
        backhauls=backhauls if backhauls is not None else list(settings.backhauls),
        clock=clock,
    )
    return service, writer, directory


async def test_cycle_complet_ecrit_les_metriques(settings: Settings) -> None:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0, uptime="01:00:00")
    service, writer, directory = build_service(settings, {"pop-test": client})

    result = await service.collect_subscribers()

    assert result.ok is True
    assert result.items == 1
    assert len(writer.subscriber_rows) == 1
    _, sample = writer.subscriber_rows[0]
    assert sample.login == "dupont"
    # Premier passage : compteurs presents, mais pas encore de debit.
    assert sample.rx_bps is None
    assert sample.rx_bytes == 0
    # L'abonne a ete cree dans le referentiel avec son plan.
    assert "dupont" in directory.subscribers
    assert directory.plans[directory.subscribers["dupont"]].source.startswith("mock:")
    # Et le PoP aussi.
    assert "PoP Test" in directory.pops


async def test_deuxieme_cycle_produit_un_debit(settings: Settings) -> None:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0, uptime="01:00:00")
    clock = Clock()
    service, writer, _ = build_service(settings, {"pop-test": client}, clock=clock)

    await service.collect_subscribers()
    clock.advance(10.0)
    # 12,5 Mo montants et 25 Mo descendants en 10 s.
    client.advance("dupont", rx_delta=12_500_000, tx_delta=25_000_000, uptime="01:00:10")
    await service.collect_subscribers()

    _, sample = writer.subscriber_rows[-1]
    assert sample.rx_bps == 10_000_000.0  # upload abonne
    assert sample.tx_bps == 20_000_000.0  # download abonne


async def test_reconnexion_pppoe_ne_produit_pas_de_pic(settings: Settings) -> None:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=900_000_000, tx_byte=900_000_000, uptime="10:00:00")
    clock = Clock()
    service, writer, _ = build_service(settings, {"pop-test": client}, clock=clock)

    await service.collect_subscribers()
    clock.advance(10.0)
    client.restart_session("dupont")
    await service.collect_subscribers()

    _, sample = writer.subscriber_rows[-1]
    assert sample.rx_bps is None
    assert sample.tx_bps is None
    assert service.rates.resets_detected == 1


async def test_un_routeur_en_panne_n_empeche_pas_les_autres(settings: Settings) -> None:
    """Isolation des pannes : c'est la propriete la plus importante du multi-PoP."""
    ok_router = RouterConfig(name="pop-ok", host="192.0.2.11", password="x", pop_name="PoP OK")
    ko_router = RouterConfig(name="pop-ko", host="192.0.2.12", password="x", pop_name="PoP KO")

    ok_client = FakeRouterOsClient()
    ok_client.add_session("dupont", rx_byte=10, tx_byte=20)
    ko_client = FakeRouterOsClient()
    ko_client.raise_on_ppp = ConnectionRefusedError("routeur injoignable")

    service, writer, _ = build_service(
        settings,
        {"pop-ok": ok_client, "pop-ko": ko_client},
        routers=[ok_router, ko_router],
    )

    result = await service.collect_subscribers()

    assert result.ok is False
    assert any("pop-ko" in error for error in result.errors)
    # Les donnees du PoP sain sont bien ecrites malgre l'echec de l'autre.
    assert result.items == 1
    assert writer.subscriber_rows[0][1].login == "dupont"


async def test_plans_demandes_une_seule_fois_par_login(settings: Settings) -> None:
    """RADIUS ne doit pas etre interroge a chaque cycle de 10 s."""
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    service, _, _ = build_service(settings, {"pop-test": client})

    appels: list[list[str]] = []
    original = service.plan_provider.get_plans

    async def compter(logins):
        appels.append(list(logins))
        return await original(logins)

    service.plan_provider.get_plans = compter  # type: ignore[method-assign]

    await service.collect_subscribers()
    await service.collect_subscribers()
    await service.collect_subscribers()

    assert appels == [["dupont"]]


async def test_collecte_backhaul(settings: Settings) -> None:
    service, writer, directory = build_service(settings, {"pop-test": FakeRouterOsClient()})

    result = await service.collect_backhauls()

    assert result.ok is True
    assert result.items == 1
    _, sample = writer.backhaul_rows[0]
    assert sample.device_id == "device-1"
    assert sample.capacity_mbps is not None and sample.capacity_mbps > 0
    assert "PoP Test" in directory.pops


async def test_collecte_backhaul_sans_inventaire(settings: Settings) -> None:
    service, writer, _ = build_service(settings, {"pop-test": FakeRouterOsClient()}, backhauls=[])
    result = await service.collect_backhauls()
    assert result.ok is True and result.items == 0
    assert writer.backhaul_rows == []


async def test_refresh_plans_met_a_jour_le_referentiel(settings: Settings) -> None:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    service, _, directory = build_service(settings, {"pop-test": client})

    await service.collect_subscribers()
    result = await service.refresh_plans()

    assert result.ok is True
    assert result.items == 1
    assert directory.plans[directory.subscribers["dupont"]].down_mbps > 0


async def test_sessions_disparues_liberees_de_la_memoire(settings: Settings) -> None:
    client = FakeRouterOsClient()
    client.add_session("a", rx_byte=0, tx_byte=0)
    client.add_session("b", rx_byte=0, tx_byte=0)
    service, _, _ = build_service(settings, {"pop-test": client})

    await service.collect_subscribers()
    assert len(service.rates) == 2

    client.active = [row for row in client.active if row["name"] == "a"]
    await service.collect_subscribers()
    assert len(service.rates) == 1


async def test_les_resultats_de_cycle_sont_historises(settings: Settings) -> None:
    service, writer, _ = build_service(settings, {"pop-test": FakeRouterOsClient()})
    await service.collect_subscribers()
    assert "collect_subscribers" in service.last_results
    assert writer.runs[-1].job == "collect_subscribers"


async def test_le_rtt_est_rattache_a_l_echantillon(settings: Settings) -> None:
    """Une seule ligne par abonne et par cycle : la latence rejoint la mesure de
    debit plutot que de creer des lignes supplementaires quasi vides."""
    from app.services.rtt import RttProber

    client = FakeRouterOsClient()
    client.ping_reply = "9ms"
    client.add_session("dupont", rx_byte=0, tx_byte=0, address="10.20.0.10")

    clock = Clock()
    prober = RttProber(batch_size=10, count=2, clock=clock)
    collectors = [MikrotikCollector(cfg, client=client) for cfg in settings.routers]
    writer = InMemoryMetricsWriter()
    service = CollectionService(
        settings,
        collectors=collectors,
        backhaul_provider=MockBackhaulProvider(clock=clock),
        plan_provider=MockPlanProvider(),
        directory=InMemoryDirectory(),
        writer=writer,
        clock=clock,
        rtt_prober=prober,
    )
    service.rtt_enabled = True  # sonde pilotee par un drapeau ; on l'active ici

    # Premier cycle : aucune sonde n'a encore tourne.
    await service.collect_subscribers()
    assert writer.subscriber_rows[-1][1].rtt_ms is None

    # La sonde tourne sur les cibles decouvertes au cycle precedent.
    result = await service.probe_rtt()
    assert result.ok and result.items == 1
    assert client.pings == [("10.20.0.10", 2)]

    # Le cycle suivant rattache la mesure.
    clock.advance(10.0)
    await service.collect_subscribers()
    assert writer.subscriber_rows[-1][1].rtt_ms == 9.0


async def test_sans_sonde_le_rtt_reste_nul(settings: Settings) -> None:
    """Comportement par defaut : la colonne existe, elle reste vide."""
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=0, tx_byte=0)
    service, writer, _ = build_service(settings, {"pop-test": client})

    await service.collect_subscribers()

    assert writer.subscriber_rows[-1][1].rtt_ms is None
    assert client.pings == []


async def test_abonne_sans_adresse_n_est_pas_sonde(settings: Settings) -> None:
    from app.services.rtt import RttProber

    client = FakeRouterOsClient()
    client.active.append({"name": "sans-ip", "uptime": "01:00:00"})
    clock = Clock()
    service = CollectionService(
        settings,
        collectors=[MikrotikCollector(cfg, client=client) for cfg in settings.routers],
        backhaul_provider=MockBackhaulProvider(clock=clock),
        plan_provider=MockPlanProvider(),
        directory=InMemoryDirectory(),
        writer=InMemoryMetricsWriter(),
        clock=clock,
        rtt_prober=RttProber(clock=clock),
    )
    service.rtt_enabled = True

    await service.collect_subscribers()
    await service.probe_rtt()

    assert client.pings == []
