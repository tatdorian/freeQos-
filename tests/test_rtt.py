"""Sonde de latence active.

Rappel : hors-bande, la mesure passive de LibreQoS (horodatages TCP) est
impossible. On sonde donc depuis le routeur, avec parcimonie.
"""

from __future__ import annotations

from app.collectors.mikrotik import MikrotikCollector
from app.config import RouterConfig
from app.services.rtt import RttProber
from tests.conftest import FakeRouterOsClient


class Horloge:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_collector(client: FakeRouterOsClient) -> MikrotikCollector:
    return MikrotikCollector(
        RouterConfig(name="pop", host="192.0.2.11", password="x"), client=client
    )


# ---------------------------------------------------------------- collecteur
async def test_ping_retourne_le_meilleur_temps() -> None:
    """On garde le minimum : c'est la latence a vide, pas la moyenne des aleas."""
    client = FakeRouterOsClient()
    client.ping_reply = "12ms"
    collector = make_collector(client)

    assert await collector.ping("10.0.0.1", count=3) == 12.0
    assert client.pings == [("10.0.0.1", 3)]


async def test_ping_sans_reponse_donne_none() -> None:
    """Un abonne qui bloque l'ICMP : aucune mesure, pas une valeur inventee."""
    client = FakeRouterOsClient()
    client.ping_reply = None
    assert await make_collector(client).ping("10.0.0.1") is None


async def test_ping_valeurs_sous_milliseconde() -> None:
    client = FakeRouterOsClient()
    client.ping_reply = "1ms500us"
    assert await make_collector(client).ping("10.0.0.1") == 1.5


# --------------------------------------------------------------------- sonde
async def test_lot_limite_et_tourniquet() -> None:
    """Le parc entier ne doit jamais etre pingue d'un coup."""
    client = FakeRouterOsClient()
    collector = make_collector(client)
    cibles = [(i, f"10.0.0.{i}", collector) for i in range(1, 11)]
    prober = RttProber(batch_size=4, clock=Horloge())

    await prober.probe(cibles)
    assert len(client.pings) == 4

    await prober.probe(cibles)
    assert len(client.pings) == 8

    # Troisieme tour : les deux derniers, puis retour au debut.
    await prober.probe(cibles)
    assert len(client.pings) == 10
    await prober.probe(cibles)
    assert len(client.pings) == 14
    assert client.pings[10][0] == "10.0.0.1"


async def test_mesure_disponible_puis_perimee() -> None:
    horloge = Horloge()
    client = FakeRouterOsClient()
    client.ping_reply = "8ms"
    prober = RttProber(batch_size=10, max_age_s=300, clock=horloge)

    await prober.probe([(1, "10.0.0.1", make_collector(client))])
    assert prober.get(1) == 8.0

    horloge.advance(299)
    assert prober.get(1) == 8.0

    # Au-dela, la mesure ne doit plus etre rattachee a un echantillon.
    horloge.advance(2)
    assert prober.get(1) is None


async def test_abonne_inconnu() -> None:
    assert RttProber(clock=Horloge()).get(999) is None


async def test_un_routeur_en_erreur_n_empeche_pas_les_autres() -> None:
    ok = FakeRouterOsClient()
    ok.ping_reply = "5ms"
    ko = FakeRouterOsClient()
    ko.ping_error = ConnectionResetError("connexion perdue")
    prober = RttProber(batch_size=10, clock=Horloge())

    answered = await prober.probe(
        [(1, "10.0.0.1", make_collector(ok)), (2, "10.0.0.2", make_collector(ko))]
    )

    assert answered == 1
    assert prober.get(1) == 5.0
    assert prober.get(2) is None


async def test_oubli_des_abonnes_deconnectes() -> None:
    """L'etat ne doit pas croitre indefiniment au fil des sessions."""
    client = FakeRouterOsClient()
    prober = RttProber(batch_size=10, clock=Horloge())
    await prober.probe(
        [(1, "10.0.0.1", make_collector(client)), (2, "10.0.0.2", make_collector(client))]
    )
    assert prober.stats()["tracked"] == 2

    prober.forget_all_but({1})
    assert prober.stats()["tracked"] == 1


async def test_sans_cible_aucun_ping() -> None:
    assert await RttProber(clock=Horloge()).probe([]) == 0
