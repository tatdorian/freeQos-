"""L'arbre reconstruit a partir de la CONFIGURATION des equipements.

LA DIFFERENCE AVEC LA DECOUVERTE PAR VOISINAGE
----------------------------------------------
``/ip/neighbor`` repond a "qui se voit ?" -- une question faible, vraie aussi de
deux equipements branches sur le meme switch. L'arbre devait donc etre DEDUIT :
racine choisie au rang, parents calcules au plus court chemin. Ces heuristiques
tombent souvent juste, mais elles ne SAVENT rien.

La configuration repond aux questions fortes, parce que c'est elle qui fait le
reseau : la route par defaut dit qui est au-dessus, une session OSPF etablie
prouve une adjacence, et l'empilement VLAN/bridge dit par quel port sort un
client. Ce fichier verifie que l'arbre s'appuie dessus.
"""

from __future__ import annotations

from pydantic import SecretStr

from app.collectors.config_graph import (
    best_upstream,
    default_gateways,
    interface_stacks,
    routing_peers,
)
from app.collectors.topology import (
    KIND_CORE,
    KIND_POP,
    TopologyLink,
    TopologyNode,
    TopologySnapshot,
    link_by_routing_adjacency,
    orient_from_config,
    reconcile_topology,
    router_node_key,
)
from app.config import RouterConfig, Settings
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient

# =========================================================================
# 1. Empilement des interfaces : par ou sort reellement le trafic
# =========================================================================


def test_un_vlan_sur_un_bridge_descend_jusqu_aux_ports() -> None:
    """Savoir qu'un client est sur vlan120 ne dit rien tant qu'on ignore que
    vlan120 est pose sur un bridge de deux ports."""
    piles = interface_stacks(
        interfaces=[{"name": n} for n in ("ether1", "ether3", "ether4")],
        vlans=[{"name": "vlan120", "interface": "bridge-acces", "vlan-id": "120"}],
        bridge_ports=[
            {"bridge": "bridge-acces", "interface": "ether3"},
            {"bridge": "bridge-acces", "interface": "ether4"},
        ],
    )
    assert piles["vlan120"].ports == ["ether3", "ether4"]
    assert piles["vlan120"].vlan_id == 120
    assert piles["vlan120"].kind == "vlan"
    assert piles["vlan120"].broken is False


def test_un_port_de_bridge_desactive_ne_porte_rien() -> None:
    piles = interface_stacks(
        bridge_ports=[
            {"bridge": "br", "interface": "ether3"},
            {"bridge": "br", "interface": "ether9", "disabled": "true"},
        ],
    )
    assert piles["br"].ports == ["ether3"]


def test_un_bonding_descend_a_ses_esclaves() -> None:
    piles = interface_stacks(bondings=[{"name": "bond1", "slaves": "ether1,ether2"}])
    assert piles["bond1"].ports == ["ether1", "ether2"]
    assert piles["bond1"].kind == "bonding"


def test_une_interface_physique_est_son_propre_port() -> None:
    piles = interface_stacks(interfaces=[{"name": "ether1"}])
    assert piles["ether1"].ports == ["ether1"]
    assert piles["ether1"].kind == "physical"


def test_un_empilement_circulaire_est_signale_pas_devine() -> None:
    """Un bridge qui se contient lui-meme : on s'arrete et on le DIT, plutot
    que de rendre une liste de ports plausible mais fausse."""
    piles = interface_stacks(
        bridge_ports=[{"bridge": "br1", "interface": "br2"}, {"bridge": "br2", "interface": "br1"}]
    )
    assert piles["br1"].broken is True


def test_un_vlan_sur_un_parent_inconnu_reste_exploitable() -> None:
    """Le parent n'a pas ete lu (interface filtree, version ancienne) : on rend
    le parent lui-meme comme port, plutot que rien."""
    piles = interface_stacks(vlans=[{"name": "vlan50", "interface": "ether7", "vlan-id": "50"}])
    assert piles["vlan50"].ports == ["ether7"]


# =========================================================================
# 2. La route par defaut : qui est au-dessus
# =========================================================================


def _defaut(gw: str, distance: str = "1", **kwargs) -> dict:
    return {
        "dst-address": "0.0.0.0/0",
        "gateway": gw,
        "distance": distance,
        "active": "true",
        **kwargs,
    }


def test_la_route_par_defaut_donne_l_amont() -> None:
    amont, raison = best_upstream(
        [_defaut("10.0.1.1"), {"dst-address": "10.20.0.0/24", "gateway": "10.0.5.2"}]
    )
    assert amont is not None
    assert amont.gateway == "10.0.1.1"
    assert raison == "route par defaut"


def test_une_route_de_secours_ne_decrit_pas_la_topologie() -> None:
    """Elle dit ce qui se passerait en cas de panne, pas ce qui se passe."""
    amont, _ = best_upstream([_defaut("10.0.1.1"), _defaut("10.0.2.1", "10", active="false")])
    assert amont.gateway == "10.0.1.1"


def test_une_route_desactivee_est_ignoree() -> None:
    assert best_upstream([_defaut("10.0.1.1", disabled="true")])[0] is None


def test_un_routeur_multi_homé_n_a_pas_un_parent() -> None:
    """Deux passerelles a egalite : un arbre n'a qu'un parent, et en designer
    un au hasard donnerait un rattachement qui change a chaque cycle."""
    amont, raison = best_upstream([_defaut("10.0.1.1"), _defaut("10.0.2.1")])
    assert amont is None
    assert raison == "ambigu"


def test_sans_route_par_defaut_on_le_dit() -> None:
    amont, raison = best_upstream([{"dst-address": "10.0.0.0/8", "gateway": "10.0.1.1"}])
    assert amont is None
    assert raison == "aucune route par defaut"


def test_la_sortie_collee_a_la_passerelle_est_nettoyee() -> None:
    """RouterOS 7 ecrit '10.0.1.1%ether1' dans immediate-gw."""
    passerelles = default_gateways(
        [{"dst-address": "0.0.0.0/0", "immediate-gw": "10.0.1.1%ether1", "active": "true"}]
    )
    assert [p.gateway for p in passerelles] == ["10.0.1.1"]
    assert passerelles[0].interface == "ether1"


# =========================================================================
# 3. Les adjacences de routage : des liens prouves
# =========================================================================


def test_seules_les_adjacences_etablies_comptent() -> None:
    """Un voisin OSPF en 'Init' n'echange encore rien : ce n'est pas un lien."""
    pairs = routing_peers(
        ospf_neighbors=[
            {"address": "10.0.1.1", "state": "Full"},
            {"address": "10.0.9.9", "state": "Init"},
        ],
        bgp_sessions=[
            {"remote.address": "10.255.0.1", "established": "true"},
            {"remote.address": "10.255.0.9", "established": "false"},
        ],
    )
    assert pairs == ["10.0.1.1", "10.255.0.1"]


def test_l_etat_2_way_reste_une_adjacence() -> None:
    """Sur un segment diffuse, deux routeurs non designes restent en 2-Way et
    se parlent reellement."""
    assert routing_peers(ospf_neighbors=[{"address": "10.0.1.1", "state": "2-Way"}]) == ["10.0.1.1"]


def test_une_adjacence_marque_un_lien_existant_sans_le_dupliquer() -> None:
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A", kind=KIND_CORE))
    snapshot.add_node(TopologyNode(key="router:b", name="B", kind=KIND_POP))
    snapshot.add_link(
        TopologyLink(
            source_key="router:a", target_key="router:b", kind="ethernet", interface="ether1"
        )
    )

    ajoutes = link_by_routing_adjacency(snapshot, {"a": ["10.0.1.2"]}, {"10.0.1.2": "router:b"})

    assert ajoutes == 0
    assert len(snapshot.links) == 1
    assert next(iter(snapshot.links.values())).attributes["routing_adjacency"] is True


def test_une_adjacence_sans_lien_connu_en_cree_un() -> None:
    """Un lien par tunnel ou par L2 opaque n'apparait dans aucun voisinage :
    la session de routage est alors la seule preuve qu'il existe."""
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A"))
    snapshot.add_node(TopologyNode(key="router:b", name="B"))

    assert (
        link_by_routing_adjacency(snapshot, {"a": ["10.255.0.2"]}, {"10.255.0.2": "router:b"}) == 1
    )
    assert len(snapshot.links) == 1


# =========================================================================
# 4. L'orientation de l'arbre
# =========================================================================


def _deux_routeurs() -> TopologySnapshot:
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key=router_node_key("core"), name="Coeur", kind=KIND_CORE))
    snapshot.add_node(TopologyNode(key=router_node_key("pop"), name="PoP", kind=KIND_POP))
    return snapshot


def test_la_route_par_defaut_pose_le_parent() -> None:
    snapshot = _deux_routeurs()
    poses, alertes = orient_from_config(
        snapshot, {"pop": ("10.0.1.1", "route par defaut")}, {"10.0.1.1": router_node_key("core")}
    )
    assert poses == 1
    assert alertes == []
    noeud = snapshot.nodes[router_node_key("pop")]
    assert noeud.config_parent == router_node_key("core")
    assert noeud.attributes["config_parent_via"] == "10.0.1.1"


def test_un_amont_hors_inventaire_ne_produit_pas_de_parent() -> None:
    """C'est le cas NORMAL de la passerelle du reseau : son amont est le
    transit, qui n'est pas un equipement gere. Silence, pas avertissement."""
    snapshot = _deux_routeurs()
    poses, alertes = orient_from_config(snapshot, {"core": ("203.0.113.1", "route par defaut")}, {})
    assert poses == 0
    assert alertes == []


def test_un_routeur_ambigu_est_signale() -> None:
    snapshot = _deux_routeurs()
    _, alertes = orient_from_config(snapshot, {"pop": (None, "ambigu")}, {})
    assert any("plusieurs routes par defaut" in a for a in alertes)


def test_une_route_qui_pointe_sur_soi_meme_est_refusee() -> None:
    snapshot = _deux_routeurs()
    poses, alertes = orient_from_config(
        snapshot,
        {"pop": ("10.9.9.9", "route par defaut")},
        {"10.9.9.9": router_node_key("pop")},
    )
    assert poses == 0
    assert any("pointe vers lui-meme" in a for a in alertes)


def test_le_parent_prouve_suit_la_fusion_des_doublons() -> None:
    """Le parent designe une case qui peut etre fusionnee juste apres. Sans
    recablage il pointerait dans le vide, et l'arbre retomberait silencieusement
    sur son calcul de plus court chemin."""
    noeuds = [
        {
            "key": "router:core",
            "name": "Coeur",
            "kind": KIND_CORE,
            "mac": "AA:AA:AA:AA:AA:AA",
            # Un routeur gere expose toujours ces deux choses : c'est par elles
            # que sa vue "voisin" se replie dans sa case.
            "attributes": {"managed": True, "macs": ["AA:AA:AA:AA:AA:AA"]},
        },
        {
            "key": "mac:AA:AA:AA:AA:AA:AA",
            "name": "Coeur",
            "kind": KIND_CORE,
            "mac": "AA:AA:AA:AA:AA:AA",
        },
        {
            "key": "router:pop",
            "name": "PoP",
            "kind": KIND_POP,
            "config_parent": "mac:AA:AA:AA:AA:AA:AA",
        },
    ]
    fusionnes, _ = reconcile_topology(noeuds, [])

    pop = next(n for n in fusionnes if n["key"] == "router:pop")
    canoniques = {n["key"] for n in fusionnes}
    assert pop["config_parent"] in canoniques
    assert pop["config_parent"] != "mac:AA:AA:AA:AA:AA:AA"


# =========================================================================
# 5. De bout en bout : un anneau, la ou l'heuristique se trompe
# =========================================================================


def _config(**kwargs) -> RouterConfig:
    base = {"name": "r", "host": "10.0.0.1", "username": "u", "password": SecretStr("p")}
    return RouterConfig(**{**base, **kwargs})


def _routeur(
    nom: str,
    loopback: str,
    adresses: list[dict],
    routes: list[dict],
    ospf: list[dict] | None = None,
) -> FakeRouterOsClient:
    client = FakeRouterOsClient(identity=nom)
    client.address_rows = [{"address": f"{loopback}/32", "interface": "lo"}] + adresses
    client.route_rows = routes
    client.ospf_neighbor_rows = ospf or []
    client.ethernet_rows = [{"name": f"ether{i}", "speed": "10Gbps"} for i in range(1, 4)]
    return client


async def test_un_anneau_ne_pend_pas_un_pop_sous_son_frere() -> None:
    """LE cas ou l'arbre devine se trompe.

    Deux PoPs relies au coeur ET entre eux. Rien dans le graphe ne dit lequel
    des deux chemins est le bon : les deux ont la meme longueur. Les tables de
    routage, elles, sont formelles -- les deux sortent par le coeur.
    """
    clients = {
        "gw": _routeur(
            "gw",
            "10.255.0.1",
            [{"address": "10.0.0.1/30", "interface": "ether1"}],
            [{"dst-address": "0.0.0.0/0", "gateway": "203.0.113.1", "active": "true"}],
        ),
        "core": _routeur(
            "core",
            "10.255.0.2",
            [
                {"address": "10.0.0.2/30", "interface": "ether1"},
                {"address": "10.0.1.1/30", "interface": "ether2"},
                {"address": "10.0.2.1/30", "interface": "ether3"},
            ],
            [_defaut("10.0.0.1")],
        ),
        "pop-nord": _routeur(
            "pop-nord",
            "10.255.0.10",
            [
                {"address": "10.0.1.2/30", "interface": "ether1"},
                {"address": "10.0.9.1/30", "interface": "ether2"},
            ],
            [_defaut("10.0.1.1")],
            ospf=[{"address": "10.0.9.2", "state": "Full"}],
        ),
        "pop-sud": _routeur(
            "pop-sud",
            "10.255.0.11",
            [
                {"address": "10.0.2.2/30", "interface": "ether1"},
                {"address": "10.0.9.2/30", "interface": "ether2"},
            ],
            [_defaut("10.0.2.1")],
            ospf=[{"address": "10.0.9.1", "state": "Full"}],
        ),
    }
    configs = [
        _config(name="gw", host="1.1.1.1", role="gateway", pop_name="Gateway"),
        _config(name="core", host="1.1.1.2", role="core", pop_name="Coeur"),
        _config(name="pop-nord", host="1.1.1.3", role="pop", pop_name="PoP Nord"),
        _config(name="pop-sud", host="1.1.1.4", role="pop", pop_name="PoP Sud"),
    ]
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=configs,
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    service = ShapingService(
        settings, registry=RouterRegistry(settings, client_factory=lambda c: clients[c.name])
    )
    await service.registry.reload()

    snapshot = await service.discover()

    parent = {n.name: n.config_parent for n in snapshot.nodes.values()}
    assert parent["Coeur"] == router_node_key("gw")
    # Les DEUX pendent du coeur, pas l'un de l'autre.
    assert parent["PoP Nord"] == router_node_key("core")
    assert parent["PoP Sud"] == router_node_key("core")
    # La passerelle n'a pas de parent : son amont est hors inventaire.
    assert parent["Gateway"] is None

    # Le lien de l'anneau existe, et il est marque prouve par OSPF.
    anneau = [
        lien
        for lien in snapshot.links.values()
        if {snapshot.nodes[lien.source_key].name, snapshot.nodes[lien.target_key].name}
        == {"PoP Nord", "PoP Sud"}
    ]
    assert anneau and anneau[0].attributes.get("routing_adjacency") is True


async def test_sans_table_de_routage_lisible_rien_ne_casse() -> None:
    """Un routeur qui refuse /ip/route (droits, version) doit simplement
    retomber sur l'arbre deduit, pas faire echouer la decouverte."""
    client = FakeRouterOsClient(identity="pop")
    client.address_rows = [{"address": "10.255.0.10/32", "interface": "lo"}]
    client.ethernet_rows = [{"name": "ether1", "speed": "1Gbps"}]
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=[_config(name="pop", host="1.1.1.3", role="pop", pop_name="PoP")],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    service = ShapingService(
        settings, registry=RouterRegistry(settings, client_factory=lambda c: client)
    )
    await service.registry.reload()

    snapshot = await service.discover()

    assert snapshot.nodes[router_node_key("pop")].config_parent is None


# =========================================================================
# 6. Les clients suivent leur VLAN jusqu'au bon lien
# =========================================================================


async def test_un_client_statique_est_rattache_par_son_vlan() -> None:
    """Sans secteur declare, un client pendait a la racine et echappait au
    partage du lien qu'il sature pourtant. La configuration sait par ou il sort.
    """
    from app.models import StaticClient
    from tests.test_clients_statiques import InventaireMemoire
    from tests.test_shaping_service import DepotBoosts, MetriquesMinimales

    client = FakeRouterOsClient(identity="pop")
    client.address_rows = [
        {"address": "10.255.0.10/32", "interface": "lo"},
        {"address": "10.0.1.2/30", "interface": "ether1"},
    ]
    client.ethernet_rows = [
        {"name": "ether1", "speed": "1Gbps"},
        {"name": "ether3", "speed": "1Gbps"},
    ]
    client.vlan_rows = [{"name": "vlan120", "interface": "bridge-acces", "vlan-id": "120"}]
    client.bridge_port_rows = [{"bridge": "bridge-acces", "interface": "ether3"}]
    client.route_rows = [_defaut("10.0.1.1")]

    fiche = StaticClient(
        reference="mairie",
        pop_name="PoP Test",
        address="10.20.0.0/29",
        vlan=120,
        plan_down_mbps=100.0,
        plan_up_mbps=20.0,
    )

    class Depot(DepotBoosts):
        async def save_snapshot(self, snapshot):
            return {"nodes": len(snapshot.nodes), "links": len(snapshot.links)}

        async def links(self):
            # Le lien decouvert sur le port ou descend vlan120.
            return [
                {
                    "key": "l1",
                    "interface": "ether3",
                    "target_key": "mac:BB",
                    "target_name": "Secteur",
                    "discovered_by": "pop-test",
                    "capacity_mbps": 500.0,
                }
            ]

    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=[_config(name="pop-test", host="1.1.1.3", role="pop", pop_name="PoP Test")],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    service = ShapingService(
        settings,
        registry=RouterRegistry(settings, client_factory=lambda c: client),
        repository=Depot(),
        static_clients=InventaireMemoire([fiche]),
    )
    service.metrics = MetriquesMinimales([])
    await service.registry.reload()
    await service.discover()  # c'est elle qui analyse la configuration

    _, abonnes = await service.build_targets("pop-test")

    assert len(abonnes) == 1
    assert abonnes[0].parent is not None, "le client doit pendre du lien de son VLAN"


async def test_sans_decouverte_le_rattachement_retombe_sur_le_secteur() -> None:
    """L'analyse de configuration est un PLUS : sans elle, le comportement
    declaratif d'avant doit rester intact."""
    from app.models import StaticClient
    from tests.test_clients_statiques import InventaireMemoire
    from tests.test_shaping_service import DepotBoosts, MetriquesMinimales

    client = FakeRouterOsClient(identity="pop")
    client.ethernet_rows = [{"name": "ether1", "speed": "1Gbps"}]
    fiche = StaticClient(
        reference="mairie", pop_name="PoP Test", address="10.20.0.5", vlan=120, plan_down_mbps=100.0
    )
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=[_config(name="pop-test", host="1.1.1.3", role="pop", pop_name="PoP Test")],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    service = ShapingService(
        settings,
        registry=RouterRegistry(settings, client_factory=lambda c: client),
        repository=DepotBoosts(),
        static_clients=InventaireMemoire([fiche]),
    )
    service.metrics = MetriquesMinimales([])
    await service.registry.reload()

    _, abonnes = await service.build_targets("pop-test")

    assert len(abonnes) == 1
    assert abonnes[0].parent is None
