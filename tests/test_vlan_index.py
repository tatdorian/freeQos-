"""Un client declare par VLAN possede le reseau de sa VLAN.

Constate : 100.100.105.242 (routeur d'un client sur vlan2060) s'affichait
"undeclared" alors que la table d'adresses du NAS, le numero de VLAN de
l'interface et la fiche du client etaient tous connus.
"""

from __future__ import annotations

import ipaddress

from app.collectors import mikrotik
from app.services.flows import PrefixIndex
from app.services.vlan_index import vlan_prefixes, where_is

RESEAUX = {
    "nas-fp": [
        (ipaddress.ip_network("100.100.105.240/30"), "vlan2060"),
        (ipaddress.ip_network("100.100.106.0/24"), "vlan2070"),
        (ipaddress.ip_network("10.0.1.0/30"), "ether1"),
    ]
}
VLANS = {"nas-fp": {"vlan2060": 2060, "vlan2070": 2070}}


def test_le_reseau_de_la_vlan_revient_a_son_seul_client() -> None:
    blocs = vlan_prefixes([(7, 2060, {"nas-fp"})], VLANS, RESEAUX)
    assert blocs == [("100.100.105.240/30", 7)]
    assert PrefixIndex.build(blocs).lookup("100.100.105.242") == 7


def test_deux_clients_sur_la_meme_vlan_on_ne_devine_pas() -> None:
    blocs = vlan_prefixes([(7, 2070, {"nas-fp"}), (8, 2070, {"nas-fp"})], VLANS, RESEAUX)
    assert blocs == []


def test_la_meme_vlan_sur_un_autre_routeur_n_est_pas_celle_du_client() -> None:
    assert vlan_prefixes([(7, 2060, {"nas-autre"})], VLANS, RESEAUX) == []


def test_l_adresse_saisie_garde_la_priorite() -> None:
    index = PrefixIndex.build([("100.100.105.240/30", 7), ("100.100.105.242/32", 9)])
    assert index.lookup("100.100.105.242") == 9


def test_une_adresse_sans_fiche_est_situee() -> None:
    mikrotik._RESEAUX_CONNECTES.clear()
    mikrotik.remember_addresses(
        "nas-fp", [{"address": "100.100.105.241/30", "interface": "vlan2060"}]
    )
    assert where_is("100.100.105.242") == {"router": "nas-fp", "interface": "vlan2060"}
    assert where_is("8.8.8.8") is None
    mikrotik._RESEAUX_CONNECTES.clear()
