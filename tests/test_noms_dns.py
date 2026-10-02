"""Le nom DEMANDE par le client, et la resolution qui n'est pas une destination.

Constate chez l'exploitant : un "ping syit.fr" s'affichait comme deux
conversations -- 8.8.8.8 (dns.google, la question) et 5.135.23.164 sous le
nom de l'hebergeur (cluster100.hosting.ovh.net). Le nom tape n'apparaissait
nulle part.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.collectors.netflow import Flow
from app.services.dns_names import DnsNames, names_from_cache
from app.services.flows import RESEAUX_CLIENTS_PAR_DEFAUT, FlowAggregator, PrefixIndex


def test_routeros_7_les_cname_remontent_jusqu_au_nom_tape() -> None:
    rows = [
        {"name": "syit.fr", "type": "A", "data": "5.135.23.164", "ttl": "1h"},
        {"name": "www.netflix.com", "type": "CNAME", "data": "www.dradis.netflix.com"},
        {"name": "www.dradis.netflix.com", "type": "CNAME", "data": "ipv4-c001.oca.nflxvideo.net"},
        {"name": "ipv4-c001.oca.nflxvideo.net", "type": "A", "data": "45.57.40.1"},
    ]
    noms = names_from_cache(rows)
    assert noms["5.135.23.164"] == "syit.fr"
    assert noms["45.57.40.1"] == "www.netflix.com"


def test_routeros_6_sans_type_ni_data() -> None:
    assert names_from_cache([{"name": "syit.fr.", "address": "5.135.23.164"}]) == {
        "5.135.23.164": "syit.fr"
    }


def test_une_boucle_de_cname_ne_bloque_pas() -> None:
    rows = [
        {"name": "a.x", "type": "CNAME", "data": "b.x"},
        {"name": "b.x", "type": "CNAME", "data": "a.x"},
        {"name": "b.x", "type": "A", "data": "192.0.2.9"},
    ]
    assert names_from_cache(rows)["192.0.2.9"] in {"a.x", "b.x"}


async def test_le_cache_de_chaque_routeur_est_relu_et_annote_les_lignes() -> None:
    class Client:
        def dns_cache(self) -> list[dict[str, Any]]:
            return [{"name": "syit.fr", "type": "A", "data": "5.135.23.164"}]

    class Muet:
        def dns_cache(self) -> list[dict[str, Any]]:
            raise TimeoutError("pas de reponse")

    noms = DnsNames()
    await noms.refresh(
        [
            SimpleNamespace(name="nas-a", _mesure=Client()),
            SimpleNamespace(name="nas-b", _mesure=Muet()),
        ]
    )
    assert noms.routers_read == 1 and "nas-b" in (noms.last_error or "")
    lignes = noms.annotate(
        [
            {"address": "5.135.23.164"},
            {"address": "8.8.4.4"},
            {"address": "5.135.23.164", "internal": True},
        ]
    )
    assert lignes[0]["domain"] == "syit.fr"
    assert "domain" not in lignes[1] and "domain" not in lignes[2]


def test_une_requete_dns_n_est_pas_une_destination() -> None:
    """Demander l'adresse de syit.fr a 8.8.8.8 n'est pas aller sur 8.8.8.8."""
    a = FlowAggregator(
        index=PrefixIndex.build([("100.64.1.11/32", 7)]),
        customer_networks=FlowAggregator.parse_networks(list(RESEAUX_CLIENTS_PAR_DEFAUT)),
    )
    a.add(
        Flow(
            src="100.64.1.11",
            dst="8.8.8.8",
            src_port=51000,
            dst_port=53,
            protocol=17,
            octets=70,
            packets=1,
        ),
        vantage="pop",
    )
    a.add(
        Flow(
            src="100.64.1.11",
            dst="5.135.23.164",
            src_port=0,
            dst_port=2048,
            protocol=1,
            octets=84,
            packets=1,
        ),
        vantage="pop",
    )
    assert [d.address for d in a.live_destinations(10)] == ["5.135.23.164"]
    # Le volume, lui, reste compte pour l'abonne.
    assert a.flows_matched == 2
