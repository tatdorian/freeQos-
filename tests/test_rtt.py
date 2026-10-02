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


def make_collector(client: FakeRouterOsClient, name: str = "pop") -> MikrotikCollector:
    return MikrotikCollector(
        RouterConfig(name=name, host="192.0.2.11", password="x"), client=client
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
    """Le PoP entier ne doit jamais etre pingue d'un coup."""
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


# ------------------------------------------------- un tourniquet PAR PoP
def deux_pops(
    petit: int, grand: int
) -> tuple[
    FakeRouterOsClient,
    FakeRouterOsClient,
    list[tuple[int, str, MikrotikCollector]],
]:
    """Un PoP minuscule et un PoP enorme, dans cet ordre dans la liste globale."""
    client_petit = FakeRouterOsClient()
    client_grand = FakeRouterOsClient()
    collecteur_petit = make_collector(client_petit, name="pop-petit")
    collecteur_grand = make_collector(client_grand, name="pop-grand")

    cibles: list[tuple[int, str, MikrotikCollector]] = [
        (i, f"10.1.0.{i}", collecteur_petit) for i in range(1, petit + 1)
    ]
    cibles += [(1000 + i, f"10.2.0.{i}", collecteur_grand) for i in range(1, grand + 1)]
    return client_petit, client_grand, cibles


async def test_le_petit_pop_n_attend_pas_le_tourniquet_du_grand() -> None:
    """La fraicheur d'un abonne depend de SON PoP, pas du parc entier.

    Avec un curseur unique, les 2 abonnes du petit PoP etaient sondes une fois
    puis attendaient que les 100 abonnes du grand PoP defilent -- soit 26 cycles
    a 4 par cycle. Le PoP qui grossit penalisait alors celui qui ne bouge pas.
    """
    petit, grand, cibles = deux_pops(petit=2, grand=100)
    prober = RttProber(batch_size=4, clock=Horloge())

    await prober.probe(cibles)
    # Premier cycle : le petit PoP est deja entierement couvert.
    assert [ip for ip, _ in petit.pings] == ["10.1.0.1", "10.1.0.2"]
    assert len(grand.pings) == 4

    await prober.probe(cibles)
    # Deuxieme cycle : le petit PoP repart AU DEBUT, il n'attend personne.
    assert [ip for ip, _ in petit.pings][-2:] == ["10.1.0.1", "10.1.0.2"]
    # Et le grand PoP avance a son propre rythme, sans etre ralenti non plus.
    assert len(grand.pings) == 8
    assert grand.pings[4][0] == "10.2.0.5"


async def test_la_charge_par_routeur_reste_celle_du_lot() -> None:
    """Le lot est PAR PoP : c'est le routeur qui emet le ping, donc c'est son CPU
    qu'on menage. Aucun des deux ne depasse batch_size sur un cycle."""
    petit, grand, cibles = deux_pops(petit=30, grand=100)
    prober = RttProber(batch_size=10, clock=Horloge())

    await prober.probe(cibles)

    assert len(petit.pings) == 10
    assert len(grand.pings) == 10


async def test_le_curseur_d_un_pop_disparu_est_oublie() -> None:
    """Un PoP retire de l'inventaire ne doit pas garder son curseur : il
    fausserait le tourniquet le jour ou le meme nom reapparait."""
    petit, grand, cibles = deux_pops(petit=4, grand=8)
    prober = RttProber(batch_size=2, clock=Horloge())

    await prober.probe(cibles)
    assert prober.stats()["pops"] == 2

    # Le grand PoP disparait (routeur retire, ou plus aucune session ouverte).
    restantes = [c for c in cibles if c[2].name == "pop-petit"]
    await prober.probe(restantes)

    assert prober.stats()["pops"] == 1
