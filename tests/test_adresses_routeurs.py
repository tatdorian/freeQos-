"""Les adresses de nos routeurs, reconnues dans les flux.

Constate chez l'exploitant : un ping vers le loopback d'un routeur (dont on a
l'API) s'affichait "unidentified", geolocalise a Columbus (Ohio) -- ce que
rendent les bases publiques pour un bloc qu'elles ne connaissent pas.
"""

from __future__ import annotations

from typing import Any

from app.collectors import mikrotik
from app.services.intel import IntelService
from app.services.own_network import CATEGORIE_INTERNE, SERVICE_INTERNE, mark_internal


def setup_function() -> None:
    mikrotik._PROPRIETAIRES.clear()
    mikrotik._LOOPBACKS_DETECTES.clear()


def _parc() -> None:
    mikrotik.remember_addresses(
        "nas-tailladje",
        [
            {"address": "11.11.11.254/32", "interface": "lo"},
            {"address": "10.0.1.2/30", "interface": "ether1"},
        ],
    )
    mikrotik.remember_loopback("core", "10.255.0.2")


def test_loopback_et_interfaces_de_nos_routeurs_sont_reconnus() -> None:
    _parc()
    assert mikrotik.own_address("11.11.11.254") == ("nas-tailladje", "loopback")
    assert mikrotik.own_address("10.0.1.2") == ("nas-tailladje", "ether1")
    assert mikrotik.own_address("10.255.0.2") == ("core", "loopback")
    assert mikrotik.own_address("8.8.8.8") is None


def test_la_ligne_est_reetiquetee_et_perd_sa_fausse_localisation() -> None:
    _parc()
    ligne: dict[str, Any] = {
        "address": "11.11.11.254",
        "service": None,
        "category": "unknown",
        "city": "Columbus",
        "country": "US",
        "latitude": 39.96,
        "longitude": -83.0,
    }
    assert mark_internal(ligne) is True
    assert ligne["service"] == SERVICE_INTERNE and ligne["category"] == CATEGORIE_INTERNE
    assert ligne["hostname"] == "nas-tailladje · loopback"
    assert ligne["city"] is None and ligne["latitude"] is None
    autre = {"address": "5.135.23.164", "service": "ovh"}
    assert mark_internal(autre) is False and autre["service"] == "ovh"


class File:
    def __init__(self, adresses: list[str]) -> None:
        self.adresses = adresses
        self.internes: list[tuple[str, str]] = []
        self.sauvees: list[dict[str, Any]] = []

    async def mark_internal(self, rows: list[tuple[str, str]]) -> int:
        self.internes = rows
        return len(rows)

    async def pending(self, **_: Any) -> list[str]:
        return list(self.adresses)

    async def save_intel(self, verdicts: list[dict[str, Any]]) -> int:
        self.sauvees.extend(verdicts)
        return len(verdicts)


async def test_une_adresse_de_nos_routeurs_ne_part_jamais_vers_une_source_externe() -> None:
    _parc()
    file = File(["11.11.11.254", "192.0.2.77"])
    service = IntelService(destinations=file, rdns_enabled=False)  # type: ignore[arg-type]
    await service.resolve_pending()
    # Etiquetee d'apres l'inventaire...
    assert ("11.11.11.254", "nas-tailladje · loopback") in file.internes
    # ...et jamais soumise aux sources externes.
    assert [v["address"] for v in file.sauvees] == ["192.0.2.77"]


# ---------------------------------------------------------------- dans les flux


def _agregateur() -> Any:
    from app.services.flows import RESEAUX_CLIENTS_PAR_DEFAUT, FlowAggregator, PrefixIndex

    return FlowAggregator(
        index=PrefixIndex.build([("100.64.1.11/32", 7)]),
        customer_networks=FlowAggregator.parse_networks(list(RESEAUX_CLIENTS_PAR_DEFAUT)),
        infrastructure_networks=FlowAggregator.parse_networks(["10.0.1.2/32"]),
    )


def _ping(src: str, dst: str, type_icmp: int) -> Any:
    from app.collectors.netflow import Flow

    return Flow(
        src=src, dst=dst, src_port=0, dst_port=type_icmp * 256, protocol=1, octets=84, packets=1
    )


def test_nos_routeurs_ne_sont_jamais_une_destination() -> None:
    """DEMANDE EXPLICITE : "ne mets pas les destinations comme celle-la, ca ne sert
    a rien de mettre le lien vers le routeur dans le trafic des abonnes".
    Loopback public ou adresse d'exploitation, dans les deux sens."""
    _parc()
    a = _agregateur()
    a.add(_ping("100.64.1.11", "11.11.11.254", 8), vantage="pop")
    a.add(_ping("11.11.11.254", "100.64.1.11", 0), vantage="pop")
    a.add(_ping("100.64.1.11", "10.0.1.2", 8), vantage="pop")
    assert a.live_destinations(50) == []


def test_la_liste_affichee_ecarte_l_historique_vers_nos_routeurs() -> None:
    from app.services.own_network import mark_all

    _parc()
    lignes = mark_all([{"address": "11.11.11.254"}, {"address": "5.135.23.164"}])
    assert [x["address"] for x in lignes] == ["5.135.23.164"]


def test_la_sonde_de_latence_du_routeur_ne_noie_pas_la_liste() -> None:
    _parc()
    a = _agregateur()
    a.add(_ping("11.11.11.254", "100.64.1.11", 8), vantage="pop")  # le routeur pingue
    a.add(_ping("100.64.1.11", "11.11.11.254", 0), vantage="pop")  # l'abonne repond
    assert a.live_destinations(50) == []


def test_deux_routeurs_qui_se_parlent_restent_ecartes() -> None:
    _parc()
    mikrotik.remember_addresses("core", [{"address": "10.0.1.1/30", "interface": "ether2"}])
    a = _agregateur()
    a.add(_ping("10.0.1.1", "10.0.1.2", 8), vantage="pop")
    assert a.live_destinations(50) == []
