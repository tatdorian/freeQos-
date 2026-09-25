"""Latence precise et points de saturation.

CE QUE CES TESTS FIXENT
-----------------------
1. LA LATENCE EST UNE SERIE, PAS UN SEUL PAQUET. Garder le minimum de deux
   pings mesurait le cas favorable : la gigue et les pertes d'un lien qui
   sature -- exactement ce qu'on cherche -- disparaissaient. La mediane est
   retenue, et la serie entiere (extremes, gigue, perte) reste lisible.
2. LA LATENCE SE LIT PAR SEGMENT. Mesurer depuis le PoP vers sa passerelle et
   vers internet dit si le retard est dans l'acces, entre le PoP et le coeur,
   ou au-dessus du coeur.
3. UN POINT DE SATURATION SE JUGE SUR LA PLUS PETITE CAPACITE CONNUE, et son
   cote (internet ou PoP) vient de la route par defaut, pas d'un nom.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.collectors.mikrotik import (
    MikrotikCollector,
    ping_stats_from_rows,
    remember_upstream,
    upstream_of,
)
from app.config import RouterConfig
from app.models import PingStats
from app.services.capacity import hotspot_rows
from app.services.rtt import PathProber, RttProber, summarise
from tests.conftest import FakeRouterOsClient

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


# ================================================================ la serie


def test_la_mediane_la_gigue_et_la_perte_se_lisent_dans_la_serie() -> None:
    rows = [
        {"seq": "0", "time": "10ms"},
        {"seq": "1", "time": "12ms"},
        {"seq": "2", "status": "timeout"},
        {"seq": "3", "time": "30ms"},
        {"seq": "4", "time": "11ms"},
    ]
    stats = ping_stats_from_rows(rows, 5)
    assert stats.sent == 5 and stats.received == 4
    assert stats.loss_pct == 20.0
    assert stats.median_ms == pytest.approx(11.5)
    assert stats.min_ms == 10 and stats.max_ms == 30
    # |12-10| + |30-12| + |11-30| = 39 sur trois ecarts.
    assert stats.jitter_ms == pytest.approx(13.0)


def test_un_paquet_sans_ligne_reste_un_paquet_perdu() -> None:
    """RouterOS peut s'arreter avant la fin : un paquet envoye sans ligne n'est
    pas un paquet oublie, c'est une perte."""
    stats = ping_stats_from_rows([{"seq": "0", "time": "5ms"}], 5)
    assert stats.loss_pct == 80.0


def test_aucune_reponse_donne_une_perte_totale_pas_un_zero() -> None:
    stats = PingStats(sent=5)
    assert stats.median_ms is None
    assert stats.loss_pct == 100.0


async def test_la_sonde_envoie_cinq_paquets_serres_et_garde_la_mediane() -> None:
    client = FakeRouterOsClient()
    client.ping_reply = "7ms"
    collector = MikrotikCollector(
        RouterConfig(name="pop-test", host="192.0.2.1", password="x"), client=client
    )
    sonde = RttProber(count=5, interval_ms=200)

    await sonde.probe([(1, "10.0.0.2", collector)])

    assert client.pings == [("10.0.0.2", 5)]
    assert client.ping_intervals == ["200ms"]
    detail = sonde.detail(1)
    assert detail is not None
    assert detail["median_ms"] == 7.0
    assert detail["sent"] == 5 and detail["received"] == 5
    assert detail["router"] == "pop-test"
    assert sonde.get(1) == 7.0


def test_la_latence_d_acces_d_un_pop_resume_ses_abonnes() -> None:
    series = [PingStats(sent=5, samples=(v, v, v)) for v in (5.0, 8.0, 12.0, 200.0)]
    resume = summarise(series)
    assert resume is not None
    assert resume["median_ms"] in (8.0, 12.0)
    # Le 90e centile nomme les plus mal servis, qu'une moyenne noierait.
    assert resume["p90_ms"] == 200.0
    assert resume["subscribers"] == 4


# ============================================================ par segment


async def test_chaque_routeur_sonde_sa_passerelle_et_internet() -> None:
    client = FakeRouterOsClient()
    client.ping_reply = "3ms"
    collector = MikrotikCollector(
        RouterConfig(name="pop-seg", host="192.0.2.9", password="x"), client=client
    )
    remember_upstream("pop-seg", "10.255.0.1", "ether1")
    try:
        sonde = PathProber(internet_targets=("1.1.1.1", "8.8.8.8"), count=5)
        await sonde.probe([collector])
    finally:
        remember_upstream("pop-seg", None, None)

    cibles = sorted(adresse for adresse, _ in client.pings)
    assert cibles == ["1.1.1.1", "10.255.0.1", "8.8.8.8"]
    vue = sonde.snapshot()["pop-seg"]
    assert vue["gateway"][0]["target"] == "10.255.0.1"
    assert vue["gateway"][0]["median_ms"] == 3.0
    assert [x["target"] for x in vue["internet"]] == ["1.1.1.1", "8.8.8.8"]
    assert upstream_of("pop-seg") == (None, None)


def test_l_interface_amont_se_lit_dans_immediate_gw() -> None:
    """'gateway' ne porte que l'adresse ; l'interface est apres le '%' de
    'immediate-gw'. Sans elle, aucun lien ne pouvait etre dit "amont"."""
    from app.collectors.config_graph import best_upstream

    amont, _ = best_upstream(
        [{"dst-address": "0.0.0.0/0", "gateway": "10.0.0.1", "immediate-gw": "10.0.0.1%sfp1"}]
    )
    assert amont is not None and amont.interface == "sfp1"


def test_l_api_latence_decrit_sa_methode(client: TestClient) -> None:
    corps = client.get("/api/v1/latency").json()
    assert corps["method"]["count"] == 5
    assert corps["method"]["interval_ms"] == 200
    assert corps["routers"][0]["router"] == "pop-test"


# ============================================================ saturation


def mesure(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "router_name": "pop-test",
        "interface": "ether2",
        "capacity_mbps": 1000.0,
        "peak_rx_bps": 50e6,
        "peak_tx_bps": 180e6,
        "avg_bps": 60e6,
        "samples": 100,
        "peak_rx_at": NOW,
        "peak_tx_at": NOW,
    }
    base.update(kwargs)
    return base


def test_la_capacite_retenue_est_la_plus_petite_connue() -> None:
    """Le port negocie 1 Gbps, mais la radio derriere n'en porte que 200 :
    c'est elle qui sature en premier."""
    lien = {
        "discovered_by": "pop-test",
        "interface": "ether2",
        "capacity_mbps": 200.0,
        "target_name": "BH-Nord",
        "tx_bps": 150e6,
        "rx_bps": 20e6,
        "measure_fresh": True,
    }
    [ligne] = hotspot_rows([mesure()], [lien], upstream={}, roles={})
    assert ligne["capacity_mbps"] == 200.0
    assert ligne["capacity_source"] == "measured link capacity"
    assert ligne["side"] == "pop"
    # Lien aval : ce que le routeur EMET est le descendant des abonnes.
    assert ligne["peak_down_mbps"] == 180.0
    assert ligne["peak_share"] == pytest.approx(0.9)
    assert ligne["state"] == "saturated"
    assert ligne["headroom_mbps"] == 20.0


def test_le_lien_amont_d_une_passerelle_est_le_cote_internet() -> None:
    [ligne] = hotspot_rows(
        [mesure(interface="sfp1", peak_rx_bps=700e6, peak_tx_bps=90e6)],
        [],
        upstream={"pop-test": ("203.0.113.1", "sfp1")},
        roles={"pop-test": "gateway"},
    )
    assert ligne["side"] == "internet"
    # Lien amont : ce que le routeur RECOIT est le descendant.
    assert ligne["peak_down_mbps"] == 700.0
    assert ligne["state"] == "busy"


def test_le_lien_amont_d_un_pop_est_le_cote_coeur() -> None:
    [ligne] = hotspot_rows(
        [mesure(interface="ether1")],
        [],
        upstream={"pop-test": ("10.0.0.1", "ether1")},
        roles={"pop-test": "pop"},
    )
    assert ligne["side"] == "upstream"


def test_les_plus_a_risque_viennent_en_premier() -> None:
    lignes = hotspot_rows(
        [
            mesure(interface="ether3", peak_tx_bps=100e6),
            mesure(interface="ether4", peak_tx_bps=950e6),
            mesure(interface="ether5", capacity_mbps=None, peak_tx_bps=5e6),
        ],
        [],
        upstream={},
        roles={},
    )
    assert [r["interface"] for r in lignes] == ["ether4", "ether3", "ether5"]
    assert lignes[-1]["state"] == "unknown"


def test_l_api_des_points_de_saturation(client: TestClient) -> None:
    corps = client.get("/api/v1/capacity/hotspots?hours=1").json()
    [ligne] = corps["hotspots"]
    assert ligne["name"] == "BH-Nord"
    assert ligne["state"] == "saturated"  # 960 Mbps sur 1 Gbps
    assert corps["thresholds"] == {"busy": 0.70, "saturated": 0.90}


@pytest.fixture
def client(settings) -> TestClient:  # type: ignore[no-untyped-def]
    from fastapi import FastAPI

    from app.api.deps import get_container
    from app.main import register_routes
    from tests.test_api import build_container

    container = build_container(settings)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


# ================================================ la sonde ne bloque pas la collecte


def test_les_sondes_ont_leur_propre_connexion_au_routeur() -> None:
    """REGRESSION : sur la connexion commune, vingt series de pings occupaient
    le routeur vingt secondes sur trente ; la lecture des sessions depassait
    son delai et le cycle de debit n'ecrivait rien -- le trafic disparaissait
    des qu'on activait la sonde."""
    collector = MikrotikCollector(RouterConfig(name="r", host="192.0.2.1", password="x"))
    assert collector._probe_client is not None  # noqa: SLF001
    assert collector._probe_client is not collector._client  # noqa: SLF001


async def test_les_pings_partent_un_par_un_par_routeur_et_en_parallele_entre_routeurs() -> None:
    """Lancees toutes ensemble, les series bloquaient chacune un thread en
    attendant la connexion, epuisaient le reservoir partage par toute
    l'application, et les dernieres expiraient avant d'etre parties."""
    import asyncio

    from app.services.rtt import ping_per_router

    en_cours: dict[str, int] = {}
    pic: dict[str, int] = {}
    simultanes_total = {"now": 0, "max": 0}

    class Routeur:
        def __init__(self, nom: str) -> None:
            self.name = nom

        async def ping_stats(self, cible: str, count: int, *, interval_ms: int) -> PingStats:
            en_cours[self.name] = en_cours.get(self.name, 0) + 1
            pic[self.name] = max(pic.get(self.name, 0), en_cours[self.name])
            simultanes_total["now"] += 1
            simultanes_total["max"] = max(simultanes_total["max"], simultanes_total["now"])
            await asyncio.sleep(0.01)
            en_cours[self.name] -= 1
            simultanes_total["now"] -= 1
            return PingStats(sent=count, samples=(1.0,))

    a, b = Routeur("a"), Routeur("b")
    cibles = [(f"10.0.0.{i}", a) for i in range(5)] + [(f"10.1.0.{i}", b) for i in range(5)]
    resultats = await ping_per_router(cibles, 5, 200)  # type: ignore[arg-type]

    assert len(resultats) == 10 and all(isinstance(r, PingStats) for r in resultats)
    assert pic == {"a": 1, "b": 1}  # jamais deux a la fois sur un routeur
    assert simultanes_total["max"] == 2  # mais les deux routeurs en parallele


def test_la_mesure_a_sa_propre_connexion_au_routeur() -> None:
    """REGRESSION : la lecture des sessions partageait la connexion de la
    decouverte, des plafonds et du shaping. Quand l'un d'eux la tenait plus de
    quinze secondes, les abonnes restaient figes sur leurs keepalives pendant
    qu'un test de debit passait -- NetFlow le voyait, la page non."""
    collector = MikrotikCollector(RouterConfig(name="r", host="192.0.2.1", password="x"))
    connexions = {
        id(collector._client),  # noqa: SLF001
        id(collector._metrics_client),  # noqa: SLF001
        id(collector._probe_client),  # noqa: SLF001
    }
    assert len(connexions) == 3


async def test_un_client_injecte_sert_aux_trois_usages() -> None:
    client = FakeRouterOsClient()
    client.add_session("dupont", rx_byte=100, tx_byte=200)
    collector = MikrotikCollector(
        RouterConfig(name="pop-test", host="192.0.2.1", password="x"), client=client
    )
    sessions = await collector.collect()
    assert [s.login for s in sessions] == ["dupont"]
