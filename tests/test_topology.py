"""Decouverte de topologie : quel lien va ou."""

from __future__ import annotations

from app.collectors.topology import (
    KIND_POP,
    KIND_RADIO,
    KIND_SECTOR,
    KIND_UNKNOWN,
    TopologySnapshot,
    attach_uisp_devices,
    build_from_router,
    classify_platform,
    ethernet_capacity_mbps,
    map_subscribers_to_sectors,
    neighbor_node_key,
    normalize_mac,
)

VOISINS = [
    {
        "interface": "ether1",
        "identity": "gw-core",
        "mac-address": "AA:BB:CC:00:00:01",
        "address": "10.0.0.1",
        "platform": "MikroTik",
        "version": "7.21.5",
    },
    {
        "interface": "ether2",
        "identity": "BH-Nord",
        "mac-address": "DC:9F:DB:11:22:33",
        "address": "10.50.0.2",
        "platform": "Ubiquiti Networks Inc.",
        "board": "PowerBeam 5AC",
    },
]

ETHERNET = [
    {"name": "ether1", "speed": "1Gbps"},
    {"name": "ether2", "rate": "100Mbps"},
]

INTERFACES = [
    {"name": "ether1", "type": "ether"},
    {"name": "ether2", "type": "ether"},
    {"name": "<pppoe-dupont>", "type": "pppoe-in"},
]

ADRESSES = [{"interface": "ether2", "address": "10.50.0.1/30"}]


def snapshot_du_pop() -> TopologySnapshot:
    snapshot = TopologySnapshot()
    build_from_router(
        snapshot,
        router_name="pop-nord",
        pop_name="PoP Nord",
        host="10.10.0.11",
        neighbors=VOISINS,
        interfaces=INTERFACES,
        ethernet=ETHERNET,
        addresses=ADRESSES,
    )
    return snapshot


# ------------------------------------------------------------------ helpers
def test_normalisation_des_mac() -> None:
    """Sans elle, la jointure RouterOS <-> UISP echoue silencieusement."""
    attendu = "AA:BB:CC:DD:EE:FF"
    for forme in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabbccddeeff", "AA:bb:CC:dd:EE:ff"):
        assert normalize_mac(forme) == attendu
    assert normalize_mac("pas-une-mac") is None
    assert normalize_mac(None) is None


def test_classification_des_plateformes() -> None:
    assert classify_platform("MikroTik") == KIND_POP
    assert classify_platform("Ubiquiti Networks Inc.") == KIND_RADIO
    assert classify_platform("Cambium Networks") == KIND_RADIO
    assert classify_platform(None) == KIND_UNKNOWN
    assert classify_platform("", "PowerBeam 5AC") == KIND_RADIO


def test_debit_negocie_du_port() -> None:
    """C'est le plafond physique : shaper au-dessus n'aurait aucun effet."""
    assert ethernet_capacity_mbps({"speed": "1Gbps"}) == 1000.0
    assert ethernet_capacity_mbps({"rate": "100Mbps"}) == 100.0
    assert ethernet_capacity_mbps({"speed": "10Gbps"}) == 10000.0
    assert ethernet_capacity_mbps({}) is None


def test_cle_de_voisin_privilegie_la_mac() -> None:
    """La MAC survit a un renommage et permet la jointure UISP."""
    assert neighbor_node_key({"mac-address": "aa:bb:cc:dd:ee:ff"}) == "mac:AA:BB:CC:DD:EE:FF"
    assert neighbor_node_key({"identity": "gw"}) == "identity:gw"
    assert neighbor_node_key({"address": "10.0.0.1"}) == "address:10.0.0.1"


# --------------------------------------------------------- graphe depuis PoP
def test_le_pop_et_ses_voisins_sont_dans_le_graphe() -> None:
    snapshot = snapshot_du_pop()

    assert snapshot.nodes["router:pop-nord"].name == "PoP Nord"
    assert snapshot.nodes["router:pop-nord"].kind == KIND_POP
    assert snapshot.nodes["mac:AA:BB:CC:00:00:01"].name == "gw-core"
    assert snapshot.nodes["mac:DC:9F:DB:11:22:33"].kind == KIND_RADIO


def test_les_liens_portent_leur_capacite_physique() -> None:
    snapshot = snapshot_du_pop()
    par_interface = {lk.interface: lk for lk in snapshot.links.values()}

    assert par_interface["ether1"].capacity_mbps == 1000.0
    assert par_interface["ether2"].capacity_mbps == 100.0
    assert par_interface["ether2"].target_key == "mac:DC:9F:DB:11:22:33"
    assert par_interface["ether2"].discovered_by == "pop-nord"
    assert par_interface["ether2"].attributes["local_networks"] == ["10.50.0.1/30"]


def test_voisin_sans_interface_ignore() -> None:
    snapshot = TopologySnapshot()
    build_from_router(
        snapshot,
        router_name="p",
        pop_name="P",
        host="h",
        neighbors=[{"identity": "orphelin"}],
        interfaces=[],
        ethernet=[],
        addresses=[],
    )
    assert snapshot.links == {}


def test_deux_pops_partagent_le_meme_voisin() -> None:
    """Le gateway vu par deux PoPs doit etre UN seul noeud, pas deux."""
    snapshot = snapshot_du_pop()
    build_from_router(
        snapshot,
        router_name="pop-sud",
        pop_name="PoP Sud",
        host="10.10.0.12",
        neighbors=[
            {
                "interface": "ether1",
                "identity": "gw-core",
                "mac-address": "AA:BB:CC:00:00:01",
                "platform": "MikroTik",
            }
        ],
        interfaces=[],
        ethernet=[{"name": "ether1", "speed": "1Gbps"}],
        addresses=[],
    )

    gateways = [n for n in snapshot.nodes.values() if n.name == "gw-core"]
    assert len(gateways) == 1
    # Mais deux liens distincts y menent, un par PoP.
    vers_gw = [lk for lk in snapshot.links.values() if lk.target_key == "mac:AA:BB:CC:00:00:01"]
    assert {lk.discovered_by for lk in vers_gw} == {"pop-nord", "pop-sud"}


# ------------------------------------------------------------------- UISP
def test_uisp_enrichit_un_voisin_existant_par_sa_mac() -> None:
    """Un voisin MikroTik de plateforme Ubiquiti et un device UISP sont le meme
    equipement : c'est la MAC qui les rapproche."""
    snapshot = snapshot_du_pop()
    rattaches = attach_uisp_devices(
        snapshot,
        [
            {
                "identification": {
                    "id": "uisp-bh-1",
                    "mac": "dc-9f-db-11-22-33",
                    "name": "BH Nord PtP",
                    "role": "station",
                }
            }
        ],
    )

    assert rattaches == 1
    noeud = snapshot.nodes["mac:DC:9F:DB:11:22:33"]
    assert noeud.uisp_device_id == "uisp-bh-1"
    assert noeud.attributes["uisp_name"] == "BH Nord PtP"
    # Pas de doublon cree.
    assert "uisp:uisp-bh-1" not in snapshot.nodes


def test_uisp_ajoute_les_equipements_inconnus_de_mikrotik() -> None:
    """Un secteur derriere un switch n'apparait pas dans /ip/neighbor du PoP."""
    snapshot = TopologySnapshot()
    attach_uisp_devices(
        snapshot,
        [
            {
                "identification": {
                    "id": "ap-1",
                    "mac": "11:22:33:44:55:66",
                    "name": "Secteur Nord 120",
                    "role": "ap",
                }
            }
        ],
    )
    assert snapshot.nodes["mac:11:22:33:44:55:66"].kind == KIND_SECTOR


def test_lien_station_vers_ap_depuis_uisp() -> None:
    snapshot = TopologySnapshot()
    attach_uisp_devices(
        snapshot,
        [
            {"identification": {"id": "ap-1", "mac": "11:22:33:44:55:66", "role": "ap"}},
            {
                "identification": {"id": "sta-1", "mac": "77:88:99:AA:BB:CC", "role": "station"},
                "attributes": {"apDevice": {"id": "ap-1", "name": "Secteur Nord"}},
            },
        ],
    )
    liens_radio = [lk for lk in snapshot.links.values() if lk.kind == "radio"]
    assert len(liens_radio) == 1
    assert liens_radio[0].source_key == "uisp:ap-1"


# ------------------------------------- LA jointure : abonne -> secteur radio
def test_caller_id_rattache_l_abonne_a_son_secteur() -> None:
    """C'est le seul moyen de savoir par quelle antenne passe un abonne."""
    snapshot = TopologySnapshot()
    sessions = [
        {"login": "dupont", "caller_id": "77:88:99:aa:bb:cc"},
        {"login": "martin", "caller_id": "00:11:22:33:44:55"},
    ]
    stations = {"77:88:99:AA:BB:CC": "uisp:ap-1"}

    rattaches = map_subscribers_to_sectors(snapshot, sessions, stations)

    assert rattaches == 1
    assert snapshot.subscriber_sectors == {"dupont": "uisp:ap-1"}


def test_jointure_tolere_les_formats_de_mac_differents() -> None:
    """RouterOS ecrit en majuscules avec deux-points, UISP fait ce qu'il veut."""
    snapshot = TopologySnapshot()
    map_subscribers_to_sectors(
        snapshot,
        [{"login": "dupont", "caller-id": "AA-BB-CC-DD-EE-FF"}],
        {"AA:BB:CC:DD:EE:FF": "uisp:ap-9"},
    )
    assert snapshot.subscriber_sectors["dupont"] == "uisp:ap-9"


def test_aucun_rattachement_produit_un_avertissement() -> None:
    """Le silence serait pire : l'operateur doit savoir que la jointure echoue."""
    snapshot = TopologySnapshot()
    map_subscribers_to_sectors(
        snapshot, [{"login": "dupont", "caller_id": "AA:BB:CC:DD:EE:FF"}], {}
    )

    assert snapshot.subscriber_sectors == {}
    assert len(snapshot.warnings) == 1
    assert "caller-id" in snapshot.warnings[0]


def test_session_sans_caller_id_ignoree() -> None:
    snapshot = TopologySnapshot()
    assert map_subscribers_to_sectors(snapshot, [{"login": "x"}], {"AA": "y"}) == 0
