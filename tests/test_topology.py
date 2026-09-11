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
    attach_uisp_devices,
    build_from_router,
    classify_platform,
    ethernet_capacity_mbps,
    link_by_shared_subnets,
    link_by_tunnels,
    map_subscribers_to_sectors,
    neighbor_node_key,
    normalize_mac,
    parse_export,
    reconcile_topology,
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
    snapshot.add_link(TopologyLink(source_key="router:pop-a", target_key="router:pop-b",
                                   kind="ethernet", interface="ether5"))
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
    assert parse_export("") == {"addresses": [], "tunnels": [], "comments": {}}
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
        [("router:a", "a", [{"type": "eoip", "name": "eoip-sud",
                             "remote_address": "100.100.101.113"}])],
    )
    assert ajoutes == 1
    lien = next(iter(snapshot.links.values()))
    assert {lien.source_key, lien.target_key} == {"router:a", "router:b"}
    assert lien.attributes["tunnel"] == "eoip"


def test_tunnel_vers_ip_inconnue_est_ignore() -> None:
    """remote-address hors du parc (transit, Internet) : pas de lien fantome."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A", kind=KIND_POP))
    ajoutes = link_by_tunnels(
        snapshot, {}, [("router:a", "a", [{"remote_address": "8.8.8.8"}])]
    )
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
        "key": key, "name": name, "kind": KIND_POP, "mac": None, "address": None,
        "platform": None, "version": None, "uisp_device_id": None,
        "pos_x": None, "pos_y": None, "parent_override": None, "hidden": False,
        "fresh": True,
    }
    base.update(extra)
    return base


def test_reconciliation_fusionne_le_meme_routeur_vu_plusieurs_fois() -> None:
    """Le PoP gere et son apparition comme voisin du coeur sont UN routeur.

    Casses differentes (NAS-FRANCOPHONIE / NAS-francophonie), IP differentes
    (management vs lien amont) : une seule case, ses adresses rassemblees."""
    noeuds = [
        _noeud("router:NAS-FRANCOPHONIE", "NAS-FRANCOPHONIE", address="100.100.101.82"),
        _noeud("identity:NAS-francophonie", "NAS-francophonie", address="11.11.11.84"),
        _noeud("mac:AA:BB:CC:00:00:01", "NAS-FRANCOPHONIE",
               mac="AA:BB:CC:00:00:01", address="11.11.11.84"),
        _noeud("router:NAS-FRNACOPHONIE", "NAS-FRNACOPHONIE", address="11.11.11.81"),
    ]
    liens = [
        {"key": "core|e1|identity:NAS-francophonie", "source_key": "router:core",
         "target_key": "identity:NAS-francophonie", "source_name": "CORE",
         "target_name": "NAS-francophonie", "target_kind": KIND_POP,
         "interface": "ether1", "rx_bps": 1.0, "tx_bps": 2.0},
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
        _noeud("router:pop-a", "PoP A", address="10.0.0.1",
               attributes={"serial": "HFX0ABCDEF"}),
        _noeud("router:pop-b", "PoP A (bis)", address="192.168.0.1",
               attributes={"serial": "hfx0abcdef"}),  # meme serie, casse differente
    ]
    fusion, _ = reconcile_topology(noeuds, [])
    assert len(fusion) == 1
    assert set(fusion[0]["addresses"]) == {"10.0.0.1", "192.168.0.1"}


def test_le_routeur_gere_porte_son_numero_de_serie() -> None:
    snapshot = TopologySnapshot()
    build_from_router(
        snapshot, router_name="pop-a", pop_name="PoP A", host="10.0.0.1",
        neighbors=[], interfaces=[], ethernet=[], addresses=[],
        identity="NAS-a", serial="HFX0ABCDEF",
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
            "router:pop-nord", "PoP Nord", mac="48:8F:5A:00:00:11",
            attributes={"managed": True, "identity": "NAS-nord",
                        "macs": ["48:8F:5A:00:00:11", "48:8F:5A:00:00:12"]},
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
            "router:pop", "PoP Nord",
            attributes='{"macs": ["48:8F:5A:00:00:12"], "identity": "NAS-nord"}',
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
    noeuds = [
        _noeud("router:pop", "PoP Nord", address="10.0.0.1"),
        _noeud("mac:DC:9F:DB:11:22:33", "PoP Nord", mac="DC:9F:DB:11:22:33"),
        _noeud("address:fe80", "MikroTik", mac="DC:9F:DB:11:22:33", address="fe80::1"),
    ]
    fusion, _ = reconcile_topology(noeuds, [])
    # Le nom rassemble les deux premiers, le MAC y agrege le troisieme (generique).
    assert len(fusion) == 1
    assert fusion[0]["key"] == "router:pop"
    assert fusion[0]["merged_count"] == 3


def test_reconciliation_supprime_un_lien_devenu_interne() -> None:
    noeuds = [
        _noeud("router:pop", "PoP Nord"),
        _noeud("mac:DC:9F:DB:11:22:33", "PoP Nord", mac="DC:9F:DB:11:22:33"),
    ]
    liens = [
        {"key": "k", "source_key": "router:pop", "target_key": "mac:DC:9F:DB:11:22:33",
         "source_name": "PoP Nord", "target_name": "PoP Nord", "target_kind": KIND_POP,
         "interface": "e1"},
    ]
    _, fusion_liens = reconcile_topology(noeuds, liens)
    assert fusion_liens == []
