"""Decouverte de topologie : quel lien va ou."""

from __future__ import annotations

from app.collectors.topology import (
    KIND_POP,
    KIND_RADIO,
    KIND_SECTOR,
    KIND_UNKNOWN,
    TopologyLink,
    TopologyNode,
    TopologySnapshot,
    ambiguous_neighbor_macs,
    attach_uisp_devices,
    build_from_router,
    classify_platform,
    ethernet_capacity_mbps,
    link_by_shared_subnets,
    link_by_tunnels,
    map_subscribers_to_sectors,
    mark_reciprocal_links,
    neighbor_node_key,
    normalize_mac,
    parse_export,
    reconcile_topology,
    resolve_to_managed,
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


# ----------------------------- un routeur gere vu en voisin n'est pas double
def test_un_voisin_qui_est_un_routeur_gere_ne_fait_pas_de_doublon() -> None:
    """CCR DS (vu en voisin, IP 11.11.11.1) EST le routeur gere DS-CCR : on ne
    cree pas une case a cote, le lien pointe vers la case API."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:main-gw", name="MAIN GATEWAY", kind="gateway"))
    snapshot.add_node(TopologyNode(key="router:ds-ccr", name="DS-CCR", kind=KIND_POP))
    # Le voisin decouvert par la gateway : nom different, mais IP du routeur gere.
    snapshot.add_node(
        TopologyNode(
            key="mac:AA:BB:CC:00:00:09", name="CCR DS", kind=KIND_POP, address="11.11.11.1"
        )
    )
    snapshot.add_link(
        TopologyLink(
            source_key="router:main-gw",
            target_key="mac:AA:BB:CC:00:00:09",
            kind="ethernet",
            interface="ether2",
        )
    )

    replies = resolve_to_managed(
        snapshot,
        ip_owner={"11.11.11.1": "router:ds-ccr", "100.100.101.113": "router:ds-ccr"},
        mac_owner={},
        name_owner={},
    )
    assert replies == 1
    assert "mac:AA:BB:CC:00:00:09" not in snapshot.nodes
    lien = next(iter(snapshot.links.values()))
    assert {lien.source_key, lien.target_key} == {"router:main-gw", "router:ds-ccr"}


def test_un_client_non_gere_reste_une_feuille() -> None:
    """Un CPE / client (aucune IP de routeur gere) n'est PAS replie : il reste."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop", name="PoP", kind=KIND_POP))
    snapshot.add_node(
        TopologyNode(
            key="mac:DE:AD:BE:EF:00:01", name="CPE-dupont", kind="cpe", address="192.168.88.2"
        )
    )
    replies = resolve_to_managed(
        snapshot, ip_owner={"10.0.0.1": "router:pop"}, mac_owner={}, name_owner={}
    )
    assert replies == 0
    assert "mac:DE:AD:BE:EF:00:01" in snapshot.nodes


def test_resolution_par_mac_et_identite() -> None:
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A", kind=KIND_POP))
    snapshot.add_node(
        TopologyNode(
            key="mac:48:8F:5A:00:00:11",
            name="quelque-chose",
            kind=KIND_POP,
            mac="48:8F:5A:00:00:11",
        )
    )
    snapshot.add_node(TopologyNode(key="identity:routeur-a", name="Routeur-A", kind=KIND_POP))
    replies = resolve_to_managed(
        snapshot,
        ip_owner={},
        mac_owner={"48:8F:5A:00:00:11": "router:a"},
        name_owner={"routeur-a": "router:a"},
    )
    assert replies == 2
    assert list(snapshot.nodes) == ["router:a"]


# ------------------------------------------- liens deduits de la config
def test_liens_deduits_des_sous_reseaux_point_a_point() -> None:
    """Deux PoP avec une adresse sur le meme /30 sont directement relies : la
    config le prouve, meme si MNDP n'a rien vu entre eux."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop-a", name="PoP A", kind=KIND_POP))
    snapshot.add_node(TopologyNode(key="router:pop-b", name="PoP B", kind=KIND_POP))
    ajoutes = link_by_shared_subnets(
        snapshot,
        [
            ("router:pop-a", "pop-a", [{"interface": "ether5", "address": "10.50.0.1/30"}]),
            ("router:pop-b", "pop-b", [{"interface": "ether3", "address": "10.50.0.2/30"}]),
        ],
    )
    assert ajoutes == 1
    lien = next(iter(snapshot.links.values()))
    assert {lien.source_key, lien.target_key} == {"router:pop-a", "router:pop-b"}
    assert lien.attributes["config_link"] is True
    assert lien.discovered_by == "pop-a"  # debit du port rattachable


def test_config_ne_double_pas_un_lien_deja_trouve() -> None:
    """Si MNDP a deja relie les deux, on n'ajoute pas un second lien parallele."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:pop-a", name="PoP A", kind=KIND_POP))
    snapshot.add_node(TopologyNode(key="router:pop-b", name="PoP B", kind=KIND_POP))
    snapshot.add_link(
        TopologyLink(
            source_key="router:pop-a",
            target_key="router:pop-b",
            kind="ethernet",
            interface="ether5",
        )
    )
    ajoutes = link_by_shared_subnets(
        snapshot,
        [
            ("router:pop-a", "pop-a", [{"interface": "ether5", "address": "10.50.0.1/30"}]),
            ("router:pop-b", "pop-b", [{"interface": "ether3", "address": "10.50.0.2/30"}]),
        ],
    )
    assert ajoutes == 0


def test_un_grand_sous_reseau_ne_relie_pas_les_routeurs() -> None:
    """Un /24 de LAN n'est pas un lien point-a-point : on ne relie pas ses hotes."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A", kind=KIND_POP))
    snapshot.add_node(TopologyNode(key="router:b", name="B", kind=KIND_POP))
    ajoutes = link_by_shared_subnets(
        snapshot,
        [
            ("router:a", "a", [{"interface": "bridge", "address": "192.168.1.1/24"}]),
            ("router:b", "b", [{"interface": "bridge", "address": "192.168.1.2/24"}]),
        ],
    )
    assert ajoutes == 0


def test_un_reseau_moins_strict_qu_un_p2p_est_ignore() -> None:
    """Seul le point-a-point STRICT (/30, /31) prouve un lien direct. Un /29
    (segment de plusieurs equipements) n'est pas retenu : on ne devine pas."""
    snapshot = TopologySnapshot()
    for k in ("router:a", "router:b", "router:c"):
        snapshot.add_node(TopologyNode(key=k, name=k, kind=KIND_POP))
    ajoutes = link_by_shared_subnets(
        snapshot,
        [
            ("router:a", "a", [{"interface": "e1", "address": "10.0.0.1/29"}]),
            ("router:b", "b", [{"interface": "e1", "address": "10.0.0.2/29"}]),
            ("router:c", "c", [{"interface": "e1", "address": "10.0.0.3/29"}]),
        ],
    )
    assert ajoutes == 0


EXPORT = """# oct/02/2025 12:00:00 by RouterOS 7.21
# software id = ABCD-1234
#
/interface eoip
add name=eoip-sud remote-address=100.100.101.113 local-address=100.100.100.254 tunnel-id=7
/interface gre
add name=gre-nord remote-address=203.0.113.9
/ip address
add address=10.50.0.1/30 interface=ether5 network=10.50.0.0
add address=100.100.100.254/24 comment="LAN gestion" interface=bridge network=100.100.100.0
/interface ethernet
set [ find default-name=ether5 ] comment="Backhaul vers PoP Sud" name=ether5
"""


def test_parse_export_extrait_adresses_tunnels_et_commentaires() -> None:
    """L'export est LA vue complete : on en tire adresses, tunnels et libelles."""
    analyse = parse_export(EXPORT)

    adresses = {a["address"]: a for a in analyse["addresses"]}
    assert adresses["10.50.0.1/30"]["interface"] == "ether5"
    assert adresses["100.100.100.254/24"]["comment"] == "LAN gestion"

    tunnels = {t["name"]: t for t in analyse["tunnels"]}
    assert tunnels["eoip-sud"]["remote_address"] == "100.100.101.113"
    assert tunnels["eoip-sud"]["type"] == "eoip"
    assert tunnels["gre-nord"]["remote_address"] == "203.0.113.9"

    assert analyse["comments"]["ether5"] == "Backhaul vers PoP Sud"


def test_parse_export_tolere_le_vide_et_le_bruit() -> None:
    assert parse_export("") == {
        "addresses": [],
        "tunnels": [],
        "comments": {},
        "router_ids": [],
    }
    assert parse_export("nimporte quoi\n# commentaire\n/truc\nset x")["tunnels"] == []


def test_liens_par_tunnel_relient_les_deux_bouts() -> None:
    """Un tunnel dont le remote-address appartient a un autre PoP les relie, meme
    sans /30 partage ni voisinage MNDP (overlay pur)."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A", kind=KIND_POP))
    snapshot.add_node(TopologyNode(key="router:b", name="B", kind=KIND_POP))
    ip_owner = {"100.100.101.113": "router:b"}
    ajoutes = link_by_tunnels(
        snapshot,
        ip_owner,
        [
            (
                "router:a",
                "a",
                [{"type": "eoip", "name": "eoip-sud", "remote_address": "100.100.101.113"}],
            )
        ],
    )
    assert ajoutes == 1
    lien = next(iter(snapshot.links.values()))
    assert {lien.source_key, lien.target_key} == {"router:a", "router:b"}
    assert lien.attributes["tunnel"] == "eoip"


def test_tunnel_vers_ip_inconnue_est_ignore() -> None:
    """remote-address hors du parc (transit, Internet) : pas de lien fantome."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A", kind=KIND_POP))
    ajoutes = link_by_tunnels(snapshot, {}, [("router:a", "a", [{"remote_address": "8.8.8.8"}])])
    assert ajoutes == 0


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


# ------------------------------------------ reconciliation des doublons
def _noeud(key, name, **extra):
    base = {
        "key": key,
        "name": name,
        "kind": KIND_POP,
        "mac": None,
        "address": None,
        "platform": None,
        "version": None,
        "uisp_device_id": None,
        "pos_x": None,
        "pos_y": None,
        "parent_override": None,
        "hidden": False,
        "fresh": True,
    }
    base.update(extra)
    return base


def test_reconciliation_fusionne_le_meme_routeur_vu_plusieurs_fois() -> None:
    """Le PoP gere et son apparition comme voisin du coeur sont UN routeur.

    C'est la MAC exposee par le routeur gere qui replie sa vue "voisin", pas son
    nom : deux equipements homonymes dans deux sites existent, et un nom ne se
    verifie pas. Le PoP au nom fautif (FRNACOPHONIE) reste donc une case a part,
    comme il se doit -- c'est un AUTRE routeur declare."""
    noeuds = [
        _noeud(
            "router:NAS-FRANCOPHONIE",
            "NAS-FRANCOPHONIE",
            address="100.100.101.82",
            attributes={"managed": True, "macs": ["AA:BB:CC:00:00:01"]},
        ),
        _noeud(
            "identity:NAS-francophonie",
            "NAS-francophonie",
            mac="AA:BB:CC:00:00:01",
            address="11.11.11.84",
        ),
        _noeud(
            "mac:AA:BB:CC:00:00:01",
            "NAS-FRANCOPHONIE",
            mac="AA:BB:CC:00:00:01",
            address="11.11.11.84",
        ),
        _noeud(
            "router:NAS-FRNACOPHONIE",
            "NAS-FRNACOPHONIE",
            address="11.11.11.81",
            attributes={"managed": True},
        ),
    ]
    liens = [
        {
            "key": "core|e1|identity:NAS-francophonie",
            "source_key": "router:core",
            "target_key": "identity:NAS-francophonie",
            "source_name": "CORE",
            "target_name": "NAS-francophonie",
            "target_kind": KIND_POP,
            "interface": "ether1",
            "rx_bps": 1.0,
            "tx_bps": 2.0,
        },
    ]
    noeuds.append(_noeud("router:core", "CORE", kind="core"))

    fusion_noeuds, fusion_liens = reconcile_topology(noeuds, liens)
    par_cle = {n["key"]: n for n in fusion_noeuds}

    # Les trois representations de NAS-FRANCOPHONIE ont fusionne ; le typo reste.
    assert "router:NAS-FRANCOPHONIE" in par_cle
    assert "identity:NAS-francophonie" not in par_cle
    assert "mac:AA:BB:CC:00:00:01" not in par_cle
    assert "router:NAS-FRNACOPHONIE" in par_cle
    # La case canonique rassemble ses deux adresses, sans dedoubler.
    canon = par_cle["router:NAS-FRANCOPHONIE"]
    assert set(canon["addresses"]) == {"100.100.101.82", "11.11.11.84"}
    assert canon["merged_count"] == 3
    # Le lien du coeur pointe desormais sur la case canonique.
    assert fusion_liens[0]["target_key"] == "router:NAS-FRANCOPHONIE"
    # La cle du lien est conservee (la mesure de debit doit la retrouver).
    assert fusion_liens[0]["key"] == "core|e1|identity:NAS-francophonie"


def test_le_routeur_gere_porte_son_identite_et_ses_mac() -> None:
    """Un routeur gere expose son identite RouterOS et ses MAC d'interface :
    c'est ce qui permet de le reconnaitre quand un autre PoP le voit en voisin."""
    snapshot = TopologySnapshot()
    build_from_router(
        snapshot,
        router_name="pop-nord",
        pop_name="PoP Nord",
        host="10.10.0.11",
        neighbors=[],
        interfaces=[{"name": "ether1", "mac-address": "48:8F:5A:00:00:11"}],
        ethernet=[{"name": "ether1", "orig-mac-address": "48:8F:5A:00:00:12"}],
        addresses=[],
        identity="NAS-nord",
    )
    noeud = snapshot.nodes["router:pop-nord"]
    assert noeud.attributes["identity"] == "NAS-nord"
    assert noeud.attributes["macs"] == ["48:8F:5A:00:00:11", "48:8F:5A:00:00:12"]
    assert noeud.mac == "48:8F:5A:00:00:11"


def test_reconciliation_fusionne_sur_le_numero_de_serie() -> None:
    """Un meme routeur joignable sous deux adresses de gestion (donc configure en
    double par l'operateur) : le numero de serie prouve que c'est le meme materiel,
    ses adresses sont rassemblees dans UNE case."""
    noeuds = [
        _noeud("router:pop-a", "PoP A", address="10.0.0.1", attributes={"serial": "HFX0ABCDEF"}),
        _noeud(
            "router:pop-b",
            "PoP A (bis)",
            address="192.168.0.1",
            attributes={"serial": "hfx0abcdef"},
        ),  # meme serie, casse differente
    ]
    fusion, _ = reconcile_topology(noeuds, [])
    assert len(fusion) == 1
    assert set(fusion[0]["addresses"]) == {"10.0.0.1", "192.168.0.1"}


def test_le_routeur_gere_porte_son_numero_de_serie() -> None:
    snapshot = TopologySnapshot()
    build_from_router(
        snapshot,
        router_name="pop-a",
        pop_name="PoP A",
        host="10.0.0.1",
        neighbors=[],
        interfaces=[],
        ethernet=[],
        addresses=[],
        identity="NAS-a",
        serial="HFX0ABCDEF",
    )
    assert snapshot.nodes["router:pop-a"].attributes["serial"] == "HFX0ABCDEF"


def test_reconciliation_fusionne_le_pop_gere_avec_sa_vue_voisin() -> None:
    """LE bug de doublon : le PoP gere (nom d'affichage 'PoP Nord') et son
    apparition comme voisin du coeur (keye par la MAC de l'interface en face,
    nomme par son identite RouterOS) sont UN seul routeur.

    Le coeur ne voit que la MAC de l'interface tournee vers lui (``...12``), qui
    n'est pas la MAC principale du PoP (``...11``) : la fusion ne peut aboutir que
    parce que le noeud gere expose TOUTES ses MAC."""
    noeuds = [
        _noeud(
            "router:pop-nord",
            "PoP Nord",
            mac="48:8F:5A:00:00:11",
            attributes={
                "managed": True,
                "identity": "NAS-nord",
                "macs": ["48:8F:5A:00:00:11", "48:8F:5A:00:00:12"],
            },
        ),
        _noeud("mac:48:8F:5A:00:00:12", "NAS-nord", mac="48:8F:5A:00:00:12"),
    ]
    fusion, _ = reconcile_topology(noeuds, [])
    assert len(fusion) == 1
    assert fusion[0]["key"] == "router:pop-nord"
    assert fusion[0]["name"] == "PoP Nord"


def test_reconciliation_lit_les_attributs_en_json_brut() -> None:
    """``attributes`` revient parfois en JSON brut (asyncpg) : la fusion doit
    quand meme lire les MAC et l'identite qui y sont rangees."""
    noeuds = [
        _noeud(
            "router:pop",
            "PoP Nord",
            attributes='{"managed": true, "macs": ["48:8F:5A:00:00:12"]}',
        ),
        _noeud("mac:48:8F:5A:00:00:12", "NAS-nord", mac="48:8F:5A:00:00:12"),
    ]
    fusion, _ = reconcile_topology(noeuds, [])
    assert len(fusion) == 1


def test_reconciliation_ne_fusionne_pas_sur_un_nom_generique() -> None:
    """Deux equipements nommes 'MikroTik' par defaut ne sont pas le meme."""
    noeuds = [
        _noeud("mac:AA:00:00:00:00:06", "MikroTik", mac="AA:00:00:00:00:06"),
        _noeud("mac:AA:00:00:00:00:08", "MikroTik", mac="AA:00:00:00:00:08"),
    ]
    fusion, _ = reconcile_topology(noeuds, [])
    assert len(fusion) == 2


def test_reconciliation_fusionne_sur_le_mac_meme_si_le_nom_manque() -> None:
    """La MAC du routeur gere replie ses deux vues "voisin", y compris celle qui
    n'a qu'un nom generique."""
    noeuds = [
        _noeud(
            "router:pop",
            "PoP Nord",
            address="10.0.0.1",
            attributes={"managed": True, "macs": ["DC:9F:DB:11:22:33"]},
        ),
        _noeud("mac:DC:9F:DB:11:22:33", "PoP Nord", mac="DC:9F:DB:11:22:33"),
        _noeud("address:fe80", "MikroTik", mac="DC:9F:DB:11:22:33", address="fe80::1"),
    ]
    fusion, _ = reconcile_topology(noeuds, [])
    assert len(fusion) == 1
    assert fusion[0]["key"] == "router:pop"
    assert fusion[0]["merged_count"] == 3


def test_reconciliation_supprime_un_lien_devenu_interne() -> None:
    noeuds = [
        _noeud(
            "router:pop",
            "PoP Nord",
            attributes={"managed": True, "macs": ["DC:9F:DB:11:22:33"]},
        ),
        _noeud("mac:DC:9F:DB:11:22:33", "PoP Nord", mac="DC:9F:DB:11:22:33"),
    ]
    liens = [
        {
            "key": "k",
            "source_key": "router:pop",
            "target_key": "mac:DC:9F:DB:11:22:33",
            "source_name": "PoP Nord",
            "target_name": "PoP Nord",
            "target_kind": KIND_POP,
            "interface": "e1",
        },
    ]
    _, fusion_liens = reconcile_topology(noeuds, liens)
    assert fusion_liens == []


# ------------------------------------- un cable vu par ses deux bouts
def _routeur(cle: str) -> TopologyNode:
    return TopologyNode(key=cle, name=cle, kind=KIND_POP, attributes={"managed": True})


def _paire_reciproque() -> TopologySnapshot:
    """Le cas courant : deux PoPs relies par un cable, chacun voyant l'autre."""
    snapshot = TopologySnapshot()
    snapshot.add_node(_routeur("router:pop-1"))
    snapshot.add_node(_routeur("router:core-1"))
    snapshot.add_link(
        TopologyLink(
            source_key="router:pop-1",
            target_key="router:core-1",
            kind="ethernet",
            interface="ether1",
            capacity_mbps=1000.0,
            discovered_by="pop-1",
        )
    )
    snapshot.add_link(
        TopologyLink(
            source_key="router:core-1",
            target_key="router:pop-1",
            kind="ethernet",
            interface="ether5",
            capacity_mbps=10000.0,
            discovered_by="core-1",
        )
    )
    return snapshot


def test_un_cable_vu_des_deux_bouts_ne_compte_qu_une_fois() -> None:
    snapshot = _paire_reciproque()

    assert mark_reciprocal_links(snapshot) == 1

    miroirs = [lien for lien in snapshot.links.values() if lien.attributes.get("mirror_of")]
    assert len(miroirs) == 1
    # Le doublon reste EN BASE : sa cle porte peut-etre une surcharge de debit
    # ou un resserrage QoE, que sa suppression effacerait en silence.
    assert len(snapshot.links) == 2


def test_le_lien_canonique_garde_le_port_d_en_face() -> None:
    """Replier ne doit rien perdre : les deux noms de port restent lisibles."""
    snapshot = _paire_reciproque()
    mark_reciprocal_links(snapshot)

    canonique = next(
        lien for lien in snapshot.links.values() if not lien.attributes.get("mirror_of")
    )
    assert canonique.attributes["peer_interface"] in {"ether1", "ether5"}
    assert canonique.attributes["peer_interface"] != canonique.interface
    assert canonique.attributes["peer_router"] in {"pop-1", "core-1"}


def test_la_capacite_retenue_est_la_plus_basse_des_deux_bouts() -> None:
    """Convertisseur de media ou port bride : c'est le plus petit qui passe."""
    snapshot = _paire_reciproque()
    mark_reciprocal_links(snapshot)

    canonique = next(
        lien for lien in snapshot.links.values() if not lien.attributes.get("mirror_of")
    )
    assert canonique.capacity_mbps == 1000.0


def test_le_choix_du_canonique_ne_bouge_pas_d_une_decouverte_a_l_autre() -> None:
    """Sinon l'arbre et le tableau se reorganiseraient a chaque cycle."""
    premier = _paire_reciproque()
    mark_reciprocal_links(premier)
    second = _paire_reciproque()
    mark_reciprocal_links(second)

    garde = lambda s: {  # noqa: E731
        lien.key for lien in s.links.values() if not lien.attributes.get("mirror_of")
    }
    assert garde(premier) == garde(second)


def test_deux_cables_paralleles_sont_laisses_intacts() -> None:
    """Quatre liens, aucune preuve de qui repond a qui : on n'efface rien."""
    snapshot = _paire_reciproque()
    for source, cible, port, par in (
        ("router:pop-1", "router:core-1", "ether2", "pop-1"),
        ("router:core-1", "router:pop-1", "ether6", "core-1"),
    ):
        snapshot.add_link(
            TopologyLink(
                source_key=source,
                target_key=cible,
                kind="ethernet",
                interface=port,
                discovered_by=par,
            )
        )

    assert mark_reciprocal_links(snapshot) == 0
    assert not any(lien.attributes.get("mirror_of") for lien in snapshot.links.values())


def test_un_lien_vers_un_equipement_non_gere_n_est_jamais_replie() -> None:
    """Une radio ou un CPE n'a qu'un seul bout observe : rien a replier."""
    snapshot = TopologySnapshot()
    snapshot.add_node(_routeur("router:pop-1"))
    snapshot.add_node(TopologyNode(key="mac:DC:9F:DB:11:22:33", name="BH", kind=KIND_RADIO))
    snapshot.add_link(
        TopologyLink(
            source_key="router:pop-1",
            target_key="mac:DC:9F:DB:11:22:33",
            kind="ethernet",
            interface="ether2",
            discovered_by="pop-1",
        )
    )

    assert mark_reciprocal_links(snapshot) == 0


# ---------------------------------------------------------------------------
# DEUX ROUTEURS DECLARES NE SE CONFONDENT JAMAIS
#
# Sur un parc virtualise -- un laboratoire EVE-NG, des CHR deployees depuis la
# meme image -- les routeurs partagent les MAC de leurs interfaces. La
# reconciliation fusionnait dessus : quatre routeurs bien distincts devenaient
# UNE case, qui absorbait leurs adresses et leurs liens, et les trois autres
# disparaissaient pureement et simplement de l'arbre. L'exploitant les voyait
# "joignables" dans son inventaire et introuvables dans sa topologie.
# ---------------------------------------------------------------------------
def _chr_clone(nom: str, adresse: str, mac_partagee: str) -> dict:
    """Une CHR d'un parc clone : identifiants propres, MAC communes."""
    return _noeud(
        f"router:{nom}",
        nom,
        address=adresse,
        mac=mac_partagee,
        attributes={"managed": True, "macs": [mac_partagee], "identity": nom},
    )


MAC_CLONE = "50:00:00:0A:00:00"


def test_des_routeurs_clones_gardent_chacun_leur_case() -> None:
    """LE cas qui faisait disparaitre trois routeurs sur quatre."""
    noeuds = [
        _chr_clone("DS-CCR", "11.11.11.1", MAC_CLONE),
        _chr_clone("NAS-BASSORA", "11.11.11.75", MAC_CLONE),
        _chr_clone("NAS-FRANCOPHONIE", "11.11.11.81", MAC_CLONE),
        _chr_clone("NAS-TAILLADJE", "11.11.11.84", MAC_CLONE),
    ]

    fusion, _ = reconcile_topology(noeuds, [])

    assert {n["key"] for n in fusion} == {
        "router:DS-CCR",
        "router:NAS-BASSORA",
        "router:NAS-FRANCOPHONIE",
        "router:NAS-TAILLADJE",
    }
    assert all(n["merged_count"] == 1 for n in fusion)


def test_une_mac_partagee_ne_replie_plus_aucun_voisin() -> None:
    """Elle n'identifie plus personne : la rattacher au premier routeur venu
    serait un rattachement tire au sort."""
    noeuds = [
        _chr_clone("DS-CCR", "11.11.11.1", MAC_CLONE),
        _chr_clone("NAS-BASSORA", "11.11.11.75", MAC_CLONE),
        _noeud("mac:50:00:00:0A:00:00", "MikroTik", mac=MAC_CLONE, address="fe80::1"),
    ]

    fusion, _ = reconcile_topology(noeuds, [])

    assert len(fusion) == 3
    assert "mac:50:00:00:0A:00:00" in {n["key"] for n in fusion}


def test_deux_routeurs_declares_au_meme_serie_restent_distincts() -> None:
    """Meme le numero de serie ne peut pas les replier : l'exploitant les a
    declares separement, et une image clonee sans reinitialisation produit
    exactement ce symptome. Effacer un routeur de l'arbre serait pire que
    d'afficher un doublon."""
    noeuds = [
        _noeud("router:a", "PoP A", attributes={"managed": True, "serial": "HFX0ABCDEF"}),
        _noeud("router:b", "PoP B", attributes={"managed": True, "serial": "HFX0ABCDEF"}),
    ]

    fusion, _ = reconcile_topology(noeuds, [])

    assert {n["key"] for n in fusion} == {"router:a", "router:b"}


def test_le_nom_ne_fusionne_plus_rien() -> None:
    """Un nom n'est unique que par convention, et une convention ne se verifie
    pas. Deux equipements homonymes dans deux sites suffisent a tout confondre."""
    noeuds = [
        _noeud("router:pop-a", "NAS-NORD", attributes={"managed": True}),
        _noeud("autre:chose", "NAS-NORD"),
    ]

    fusion, _ = reconcile_topology(noeuds, [])

    assert len(fusion) == 2


def test_le_numero_de_serie_replie_toujours_une_vue_decouverte() -> None:
    """Ce qu'on garde : la preuve d'identite. Une case decouverte qui porte le
    meme numero de serie qu'un routeur gere EST ce routeur."""
    noeuds = [
        _noeud("router:pop", "PoP Nord", attributes={"managed": True, "serial": "HFX0ABCDEF"}),
        _noeud(
            "mac:AA:BB", "MikroTik", mac="AA:BB:CC:DD:EE:FF", attributes={"serial": "hfx0abcdef"}
        ),
    ]

    fusion, _ = reconcile_topology(noeuds, [])

    assert len(fusion) == 1
    assert fusion[0]["key"] == "router:pop"


# ---------------------------------------------------------------------------
# UNE MAC PARTAGEE NE PEUT PAS SERVIR DE CLE
#
# ``neighbor_node_key`` keyait un voisin sur sa MAC. Quand plusieurs voisins
# annoncent la MEME -- machines virtuelles clonees -- ils recevaient tous LA
# MEME CLE et s'ecrasaient en un seul noeud des l'ajout au graphe, avant toute
# reconciliation. Tous les liens du reseau aboutissaient alors au meme endroit,
# et l'arbre montrait une chaine la ou il y avait une etoile.
# ---------------------------------------------------------------------------
def _voisin(identite: str, mac: str, ip: str = "") -> dict:
    return {"interface": "ether1", "identity": identite, "mac-address": mac, "address": ip}


def test_une_mac_annoncee_par_plusieurs_identites_est_reperee() -> None:
    ambigues = ambiguous_neighbor_macs(
        [
            _voisin("NAS-BASSORA", "50:00:00:0A:00:00"),
            _voisin("NAS-TAILLADJE", "50:00:00:0A:00:00"),
            _voisin("BH-Nord", "DC:9F:DB:11:22:33"),
        ]
    )
    assert ambigues == {"50:00:00:0A:00:00"}


def test_le_meme_equipement_vu_deux_fois_n_est_pas_ambigu() -> None:
    """Un voisin vu par deux routeurs annonce la meme MAC ET la meme identite :
    c'est bien un seul equipement, sa MAC reste une cle valable."""
    assert (
        ambiguous_neighbor_macs(
            [_voisin("BH-Nord", "DC:9F:DB:11:22:33"), _voisin("BH-Nord", "DC:9F:DB:11:22:33")]
        )
        == set()
    )


def test_un_nom_generique_ne_rend_pas_une_mac_ambigue() -> None:
    """Deux 'MikroTik' sur la meme MAC ne prouvent pas deux equipements : le nom
    par defaut ne distingue rien, et on ne casserait la cle que sur du bruit."""
    assert (
        ambiguous_neighbor_macs(
            [_voisin("MikroTik", "DC:9F:DB:11:22:33"), _voisin("MikroTik", "DC:9F:DB:11:22:33")]
        )
        == set()
    )


def test_une_mac_ambigue_cede_la_cle_a_l_identite() -> None:
    """LE correctif : sans cela, ces deux voisins recevaient la meme cle."""
    ambigues = {"50:00:00:0A:00:00"}
    a = neighbor_node_key(_voisin("NAS-BASSORA", "50:00:00:0A:00:00"), ambigues)
    b = neighbor_node_key(_voisin("NAS-TAILLADJE", "50:00:00:0A:00:00"), ambigues)

    assert a != b
    assert a == "identity:NAS-BASSORA"


def test_une_mac_unique_reste_la_cle() -> None:
    """Le cas normal ne change pas : la MAC survit a un changement de nom ou
    d'adresse, et c'est elle qui joint le graphe a UISP."""
    assert neighbor_node_key(_voisin("BH-Nord", "DC:9F:DB:11:22:33"), set()) == (
        "mac:DC:9F:DB:11:22:33"
    )


def test_une_mac_ambigue_sans_identite_garde_la_mac() -> None:
    """Faute de mieux : une cle imparfaite vaut mieux qu'aucune."""
    assert (
        neighbor_node_key(
            {"interface": "e1", "mac-address": "50:00:00:0A:00:00"}, {"50:00:00:0A:00:00"}
        )
        == "mac:50:00:00:0A:00:00"
    )
