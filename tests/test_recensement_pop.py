"""Recenser TOUS les clients d'un PoP, y compris ceux qu'aucune VLAN ne nomme.

CE QUE CE FICHIER VERROUILLE
----------------------------
La detection historique posait une question fragile : "l'entree ARP est-elle
rattachee a une /interface/vlan ?". Sur un pont en filtrage VLAN -- le montage
le plus repandu des que le PoP commute -- la reponse est non, et le client
disparaissait. Les tests ci-dessous verrouillent le renversement : on part du
PLAN D'ADRESSAGE du PoP, on croise sept sources de presence, et on AVOUE ce
qu'on ne sait pas.

La derniere section est la plus importante : un recensement qui grossit la
liste sans garde-fou serait un recul. Un abonne PPPoE, une antenne, le routeur
lui-meme ne doivent JAMAIS etre proposes comme clients a declarer.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.collectors.mikrotik import MikrotikCollector
from app.collectors.pop_census import (
    NATURE_CLIENT,
    NATURE_EQUIPEMENT,
    NATURE_HORS_PERIMETRE,
    NATURE_PPPOE,
    ROLE_CLIENT,
    ROLE_POINT_A_POINT,
    ROLE_PPPOE,
    ROLE_TRANSIT,
    SOURCE_ARP,
    SOURCE_BAIL,
    SOURCE_FILE,
    SOURCE_ROUTE,
    VLAN_PAR_INTERFACE,
    VLAN_PAR_PONT,
    VLAN_PAR_PVID,
    PartialCensusError,
    RouterTables,
    build_census,
    classify_subnets,
    client_networks,
    sightings_from_census,
)
from app.config import Settings
from app.main import register_routes
from tests.conftest import FakeRouterOsClient
from tests.test_api import build_container
from tests.test_clients_statiques import InventaireApi

# =========================================================================
# Un PoP realiste : un pont en filtrage VLAN, une VLAN classique, du PPPoE,
# un transit, un loopback. C'est exactement la configuration qui mettait la
# detection en defaut.
# =========================================================================

ADRESSES = [
    {"address": "192.0.2.2/30", "interface": "ether1", "network": "192.0.2.0"},
    {"address": "10.20.0.1/24", "interface": "bridge-clients", "network": "10.20.0.0"},
    {"address": "10.30.0.1/24", "interface": "vlan130", "network": "10.30.0.0"},
    {"address": "10.99.0.1/24", "interface": "vlan999", "network": "10.99.0.0"},
    {"address": "10.255.0.1/32", "interface": "lo", "network": "10.255.0.1"},
]
VLANS = [
    {"name": "vlan130", "vlan-id": "130", "interface": "bridge-clients"},
    {"name": "vlan999", "vlan-id": "999", "interface": "bridge-clients"},
]
SERVEURS_PPPOE = [{"interface": "vlan999", "service-name": "isp"}]
PORTS_DE_PONT = [
    {"bridge": "bridge-clients", "interface": "ether3", "pvid": "120"},
    {"bridge": "bridge-clients", "interface": "ether4", "pvid": "121"},
]
ROUTES = [
    {"dst-address": "0.0.0.0/0", "gateway": "192.0.2.1", "distance": "1", "active": "true"},
    # Un /29 vendu a une entreprise, route derriere son routeur : AUCUNE table
    # de presence ne montre ce bloc, seule la passerelle parle.
    {"dst-address": "10.31.0.0/29", "gateway": "10.30.0.9", "static": "true"},
]


def _tables(**surcharges: object) -> RouterTables:
    base: dict[str, object] = {
        "addresses": ADRESSES,
        "vlans": VLANS,
        "pppoe_servers": SERVEURS_PPPOE,
        "bridge_ports": PORTS_DE_PONT,
        "routes": ROUTES,
    }
    base.update(surcharges)
    return RouterTables(**base)  # type: ignore[arg-type]


def _recense(**surcharges: object):
    return build_census(_tables(**surcharges), router_name="pop-test", pop_name="PoP Test")


def _hote(recensement, adresse: str):
    for hote in recensement.hosts:
        if hote.address == adresse:
            return hote
    raise AssertionError(
        f"{adresse} absente du recensement : {[h.address for h in recensement.hosts]}"
    )


# =========================================================================
# 1. LE PERIMETRE : ou un client peut se trouver, et pourquoi
# =========================================================================


def test_le_perimetre_sort_de_l_adressage_pas_du_nom_des_interfaces() -> None:
    """LA BASCULE. Un pont qui porte 10.20.0.1/24 dessert des clients, meme si
    aucune interface ne s'appelle 'vlan quelque chose'."""
    reseaux = classify_subnets(
        addresses=ADRESSES,
        vlans=VLANS,
        pppoe_servers=SERVEURS_PPPOE,
        routes=ROUTES,
    )
    par_reseau = {s.network: s for s in reseaux}

    assert par_reseau["10.20.0.0/24"].role == ROLE_CLIENT
    assert par_reseau["10.20.0.0/24"].interface == "bridge-clients"
    assert par_reseau["10.30.0.0/24"].role == ROLE_CLIENT


def test_un_lien_point_a_point_n_est_pas_de_la_desserte() -> None:
    """Personne n'a jamais vendu un acces dans un /30."""
    reseaux = {s.network: s for s in classify_subnets(addresses=ADRESSES, routes=ROUTES)}
    assert reseaux["192.0.2.0/30"].role == ROLE_POINT_A_POINT
    assert reseaux["10.255.0.1/32"].role == ROLE_POINT_A_POINT


def test_le_transit_est_reconnu_par_ses_adjacences() -> None:
    """Un sous-reseau qui porte un pair OSPF etabli n'est pas de la desserte,
    quelle que soit sa taille -- un /24 de transit existe."""
    reseaux = {
        s.network: s
        for s in classify_subnets(
            addresses=[{"address": "10.50.0.1/24", "interface": "ether2"}],
            ospf_neighbors=[{"address": "10.50.0.2", "state": "Full"}],
        )
    }
    assert reseaux["10.50.0.0/24"].role == ROLE_TRANSIT


def test_l_interface_a_serveur_pppoe_est_exclue_du_perimetre() -> None:
    """Ses abonnes ont deja une identite : les proposer les dedoublerait."""
    reseaux = {
        s.network: s
        for s in classify_subnets(
            addresses=ADRESSES, vlans=VLANS, pppoe_servers=SERVEURS_PPPOE, routes=ROUTES
        )
    }
    assert reseaux["10.99.0.0/24"].role == ROLE_PPPOE
    assert "10.99.0.0/24" not in client_networks(list(reseaux.values()))


def test_une_adresse_desactivee_ne_dessert_rien() -> None:
    reseaux = classify_subnets(
        addresses=[{"address": "10.60.0.1/24", "interface": "vlan160", "disabled": "true"}]
    )
    assert reseaux[0].role != ROLE_CLIENT


def test_chaque_sous_reseau_porte_le_motif_de_son_classement() -> None:
    """Un perimetre qu'on ne peut pas relire est un perimetre qu'on subit."""
    for sous_reseau in classify_subnets(addresses=ADRESSES, vlans=VLANS, routes=ROUTES):
        assert sous_reseau.reason


# =========================================================================
# 2. LE CAS QUI MOTIVAIT TOUT : le pont en filtrage VLAN
# =========================================================================


def test_un_client_derriere_un_pont_est_enfin_vu() -> None:
    """AVANT : /ip/arp nomme 'bridge-clients', l'interface n'est pas dans
    /interface/vlan, le client disparait. MAINTENANT : son adresse tombe dans
    10.20.0.0/24, que ce routeur dessert -- et cela suffit."""
    recensement = _recense(
        arp=[
            {
                "address": "10.20.0.50",
                "mac-address": "AA:BB:CC:00:00:50",
                "interface": "bridge-clients",
            }
        ]
    )
    hote = _hote(recensement, "10.20.0.50")
    assert hote.nature == NATURE_CLIENT
    assert hote.subnet == "10.20.0.0/24"
    assert SOURCE_ARP in hote.sources


def test_la_table_de_ponts_rend_le_vlan_et_le_port_que_l_arp_a_perdus() -> None:
    """Localiser, ce n'est pas seulement "il existe" : c'est le VLAN et le port.

    La jointure se fait par la MAC, seule cle commune a /ip/arp et
    /interface/bridge/host.
    """
    recensement = _recense(
        arp=[
            {
                "address": "10.20.0.50",
                "mac-address": "AA:BB:CC:00:00:50",
                "interface": "bridge-clients",
            }
        ],
        bridge_hosts=[
            {
                "mac-address": "AA:BB:CC:00:00:50",
                "on-interface": "ether3",
                "bridge": "bridge-clients",
                "vid": "120",
            }
        ],
    )
    hote = _hote(recensement, "10.20.0.50")
    assert hote.vlan_id == 120
    assert hote.vlan_source == VLAN_PAR_PONT
    assert hote.ports == ["ether3"]


def test_le_vlan_d_une_interface_declaree_reste_prioritaire() -> None:
    """Une /interface/vlan PORTE son numero ; la table de ponts ne fait que
    l'observer. Rendre la provenance permet de recouper sur le routeur."""
    recensement = _recense(
        arp=[{"address": "10.30.0.5", "mac-address": "AA:BB:CC:00:00:05", "interface": "vlan130"}]
    )
    hote = _hote(recensement, "10.30.0.5")
    assert (hote.vlan_id, hote.vlan_source) == (130, VLAN_PAR_INTERFACE)


def test_a_defaut_le_pvid_du_port_donne_le_vlan() -> None:
    """Dernier recours, et il est dit comme tel : un pvid SUPPOSE le VLAN d'un
    port d'acces, il ne l'observe pas."""
    recensement = _recense(
        addresses=[{"address": "10.20.0.1/24", "interface": "ether3"}],
        arp=[{"address": "10.20.0.60", "mac-address": "AA:BB:CC:00:00:60", "interface": "ether3"}],
    )
    hote = _hote(recensement, "10.20.0.60")
    assert (hote.vlan_id, hote.vlan_source) == (120, VLAN_PAR_PVID)


# =========================================================================
# 3. LA FUSION : chaque source couvre l'angle mort des autres
# =========================================================================


def test_un_client_muet_reste_visible_par_son_bail_dhcp() -> None:
    """Une entree ARP s'efface apres quelques minutes de silence. Un bail non.
    C'est aussi la seule source qui porte souvent un NOM."""
    recensement = _recense(
        arp=[],
        dhcp_leases=[
            {
                "address": "10.20.0.51",
                "mac-address": "AA:BB:CC:00:00:51",
                "host-name": "caisse-boulangerie",
                "server": "dhcp-clients",
                "status": "bound",
            }
        ],
    )
    hote = _hote(recensement, "10.20.0.51")
    assert hote.nature == NATURE_CLIENT
    assert hote.hostname == "caisse-boulangerie"
    assert hote.sources == [SOURCE_BAIL]


def test_le_bail_est_rattache_a_l_interface_de_son_serveur() -> None:
    """Un bail nomme son SERVEUR, pas son interface. Confondre les deux ferait
    chercher le VLAN d'une interface qui n'existe pas."""
    recensement = _recense(
        arp=[],
        dhcp_servers=[{"name": "dhcp-clients", "interface": "vlan130"}],
        dhcp_leases=[
            {"address": "10.30.0.51", "mac-address": "AA:BB:CC:00:03:51", "server": "dhcp-clients"}
        ],
    )
    hote = _hote(recensement, "10.30.0.51")
    assert hote.interface == "vlan130"
    assert (hote.vlan_id, hote.vlan_source) == (130, VLAN_PAR_INTERFACE)


def test_un_bloc_route_derriere_un_cpe_est_recense_comme_bloc() -> None:
    """LE CAS QUE L'ARP NE PEUT PAS VOIR. Un /29 vendu a une entreprise ne parle
    jamais : seule sa passerelle parle. Sans /ip/route, on shaperait une adresse
    a la place d'un client entier."""
    recensement = _recense(
        arp=[{"address": "10.30.0.9", "mac-address": "AA:BB:CC:00:00:09", "interface": "vlan130"}]
    )
    passerelle = _hote(recensement, "10.30.0.9")

    assert passerelle.routed_prefixes == ["10.31.0.0/29"]
    assert [b.prefix for b in recensement.blocks] == ["10.31.0.0/29"]
    assert recensement.blocks[0].via == "10.30.0.9"
    assert recensement.blocks[0].source == SOURCE_ROUTE


def test_la_route_par_defaut_n_est_pas_un_client() -> None:
    recensement = _recense(arp=[])
    assert all(b.prefix != "0.0.0.0/0" for b in recensement.blocks)
    assert all(h.address != "192.0.2.1" or h.nature != NATURE_CLIENT for h in recensement.hosts)


def test_une_file_deja_posee_designe_un_client_meme_totalement_muet() -> None:
    """C'est la source la plus qualifiee de toutes : un humain a decide qu'il y
    avait la un client, et l'a ecrit sur le routeur."""
    recensement = _recense(
        arp=[],
        queues=[
            {"name": "pro-machin", "target": "10.20.0.80/32", "comment": "SARL Machin"},
            {"name": "bloc-pro", "target": "10.20.0.88/29"},
        ],
    )
    hote = _hote(recensement, "10.20.0.80")
    assert hote.sources == [SOURCE_FILE]
    assert hote.comment == "SARL Machin"
    assert "10.20.0.88/29" in [b.prefix for b in recensement.blocks]


def test_une_file_hors_perimetre_n_invente_pas_de_client() -> None:
    """Une file qui vise du transit ou un equipement ne designe pas un client."""
    recensement = _recense(arp=[], queues=[{"name": "amont", "target": "192.0.2.1/32"}])
    assert all(h.address != "192.0.2.1" for h in recensement.hosts)


def test_le_voisinage_donne_une_identite_sans_creer_de_client() -> None:
    recensement = _recense(
        arp=[
            {
                "address": "10.20.0.90",
                "mac-address": "AA:BB:CC:00:00:90",
                "interface": "bridge-clients",
            }
        ],
        neighbors=[
            {
                "address": "10.20.0.90",
                "mac-address": "AA:BB:CC:00:00:90",
                "identity": "cpe-durand",
                "board": "LHG 5",
            }
        ],
    )
    hote = _hote(recensement, "10.20.0.90")
    assert hote.identity == "cpe-durand (LHG 5)"


def test_les_sources_sont_cumulees_jamais_ecrasees() -> None:
    """Savoir PAR QUOI un client est vu dit quelle confiance accorder a son
    absence au cycle suivant."""
    recensement = _recense(
        arp=[
            {
                "address": "10.20.0.51",
                "mac-address": "AA:BB:CC:00:00:51",
                "interface": "bridge-clients",
            }
        ],
        dhcp_leases=[
            {"address": "10.20.0.51", "mac-address": "AA:BB:CC:00:00:51", "host-name": "poste-1"}
        ],
    )
    hote = _hote(recensement, "10.20.0.51")
    assert set(hote.sources) == {SOURCE_ARP, SOURCE_BAIL}
    assert hote.hostname == "poste-1"


# =========================================================================
# 4. CE QUI N'EST PAS UN CLIENT : la liste ne doit pas devenir illisible
# =========================================================================


def test_un_abonne_pppoe_n_est_jamais_propose_a_la_declaration() -> None:
    """Il a deja une identite et un plan. Le proposer serait le dedoubler --
    la pire confusion possible a mettre sous les yeux d'un exploitant."""
    recensement = _recense(
        ppp_active=[{"name": "dupont", "address": "10.99.0.7", "caller-id": "AA:BB:CC:00:00:07"}],
        arp=[{"address": "10.99.0.7", "mac-address": "AA:BB:CC:00:00:07", "interface": "vlan999"}],
    )
    hote = _hote(recensement, "10.99.0.7")
    assert hote.nature == NATURE_PPPOE
    assert hote.login == "dupont"
    assert "10.99.0.7" not in [v.address for v in sightings_from_census(recensement)]


def test_le_routeur_ne_se_propose_pas_lui_meme() -> None:
    recensement = _recense(
        arp=[
            {
                "address": "10.20.0.1",
                "mac-address": "AA:BB:CC:00:00:01",
                "interface": "bridge-clients",
            }
        ]
    )
    assert _hote(recensement, "10.20.0.1").nature == NATURE_EQUIPEMENT


def test_une_antenne_declaree_reste_une_antenne_dans_une_vlan_cliente() -> None:
    """Sans cela, chaque secteur radio reapparait en client a chaque cycle."""
    tables = _tables(
        arp=[
            {
                "address": "10.20.0.240",
                "mac-address": "AA:BB:CC:00:02:40",
                "interface": "bridge-clients",
            }
        ]
    )
    recensement = build_census(
        tables, router_name="pop-test", pop_name="PoP Test", known_equipment=["10.20.0.240"]
    )
    assert _hote(recensement, "10.20.0.240").nature == NATURE_EQUIPEMENT


def test_le_pair_de_routage_n_est_pas_un_client() -> None:
    recensement = _recense(
        arp=[{"address": "192.0.2.1", "mac-address": "AA:BB:CC:00:00:FF", "interface": "ether1"}]
    )
    assert _hote(recensement, "192.0.2.1").nature != NATURE_CLIENT


def test_seuls_les_clients_possibles_deviennent_des_observations() -> None:
    """Le contrat de vlan_sightings ne bouge pas d'un iota : une observation
    reste une observation, et aucune fiche n'en sort toute seule."""
    recensement = _recense(
        ppp_active=[{"name": "dupont", "address": "10.99.0.7"}],
        arp=[
            {
                "address": "10.20.0.50",
                "mac-address": "AA:BB:CC:00:00:50",
                "interface": "bridge-clients",
            },
            {
                "address": "10.20.0.1",
                "mac-address": "AA:BB:CC:00:00:01",
                "interface": "bridge-clients",
            },
            {"address": "192.0.2.1", "mac-address": "AA:BB:CC:00:00:FF", "interface": "ether1"},
        ],
    )
    # 10.30.0.9 est la passerelle du /29 route : un client, lui aussi.
    assert [v.address for v in sightings_from_census(recensement)] == ["10.20.0.50", "10.30.0.9"]


# =========================================================================
# 5. L'AVEU : un trou qui ne se voit pas est pire qu'un trou
# =========================================================================


def test_une_source_illisible_est_dite_et_non_avalee() -> None:
    recensement = build_census(
        RouterTables(addresses=ADRESSES, unreadable={"dhcp_leases": "TimeoutError: 10s"}),
        router_name="pop-test",
        pop_name="PoP Test",
    )
    assert any("dhcp" in note.lower() for note in recensement.remarks)
    assert "illisible" in recensement.sources["/ip/dhcp-server/lease"]


def test_un_sous_reseau_client_sans_aucune_presence_est_signale() -> None:
    """Soit personne n'y parle, soit la desserte passe par un equipement qui
    masque ses clients. Les deux demandent une verification."""
    recensement = _recense(arp=[])
    assert any("10.20.0.0/24" in note for note in recensement.remarks)


def test_une_adresse_hors_de_tout_sous_reseau_connu_est_signalee() -> None:
    """Le PoP qui commute sans router : l'adressage est ailleurs, et il faut le
    dire plutot que de rendre une liste vide."""
    recensement = _recense(
        arp=[
            {
                "address": "172.16.5.9",
                "mac-address": "AA:BB:CC:00:05:09",
                "interface": "bridge-clients",
            }
        ]
    )
    assert _hote(recensement, "172.16.5.9").nature == NATURE_HORS_PERIMETRE
    assert any("hors de tout sous-reseau" in note for note in recensement.remarks)


def test_une_mac_vue_en_l2_sans_ip_est_un_trou_rendu_tel_quel() -> None:
    recensement = _recense(
        arp=[],
        bridge_hosts=[{"mac-address": "AA:BB:CC:00:0F:0F", "on-interface": "ether4", "vid": "121"}],
    )
    assert [p.mac for p in recensement.l2_only] == ["AA:BB:CC:00:0F:0F"]
    assert any("sans adresse IP" in note for note in recensement.remarks)


def test_la_mac_du_pont_lui_meme_n_est_pas_une_presence() -> None:
    recensement = _recense(
        arp=[], bridge_hosts=[{"mac-address": "AA:BB:CC:00:0B:0B", "local": "true"}]
    )
    assert recensement.l2_only == []


def test_un_client_connu_par_la_seule_arp_est_signale_comme_fragile() -> None:
    recensement = _recense(
        arp=[
            {
                "address": "10.20.0.50",
                "mac-address": "AA:BB:CC:00:00:50",
                "interface": "bridge-clients",
            }
        ]
    )
    assert any("SEULE table ARP" in note for note in recensement.remarks)


def test_sans_adressage_lisible_le_recensement_le_dit() -> None:
    """Le filet historique joue (le nom des interfaces), mais il ne suffit pas
    et cela doit se lire."""
    recensement = build_census(
        RouterTables(
            vlans=VLANS,
            arp=[
                {"address": "10.30.0.5", "mac-address": "AA:BB:CC:00:00:05", "interface": "vlan130"}
            ],
        ),
        router_name="pop-test",
        pop_name="PoP Test",
    )
    assert _hote(recensement, "10.30.0.5").nature == NATURE_CLIENT
    assert any("perimetre est vide" in note for note in recensement.remarks)


# =========================================================================
# 6. LE COLLECTEUR : lectures tolerees, degradation signalee
# =========================================================================


@pytest.fixture
def routeur() -> FakeRouterOsClient:
    client = FakeRouterOsClient()
    client.address_rows = list(ADRESSES)
    client.vlan_rows = list(VLANS)
    client.pppoe_server_rows = list(SERVEURS_PPPOE)
    client.bridge_port_rows = list(PORTS_DE_PONT)
    client.route_rows = list(ROUTES)
    client.arp_rows = [
        {
            "address": "10.20.0.50",
            "mac-address": "AA:BB:CC:00:00:50",
            "interface": "bridge-clients",
        }
    ]
    client.bridge_host_rows = [
        {
            "mac-address": "AA:BB:CC:00:00:50",
            "on-interface": "ether3",
            "bridge": "bridge-clients",
            "vid": "120",
        }
    ]
    client.dhcp_lease_rows = [
        {"address": "10.20.0.51", "mac-address": "AA:BB:CC:00:00:51", "host-name": "poste-1"}
    ]
    return client


async def test_le_collecteur_recense_sans_rien_ecrire(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    collecteur = MikrotikCollector(settings.routers[0], client=routeur)

    recensement = await collecteur.census()

    adresses = {h.address: h for h in recensement.hosts}
    assert adresses["10.20.0.50"].vlan_id == 120
    assert adresses["10.20.0.51"].hostname == "poste-1"
    assert recensement.pop_name == settings.routers[0].effective_pop_name


async def test_une_table_absente_ne_fait_pas_perdre_les_autres(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Un routeur sans serveur PPPoE n'a pas la table : ce n'est pas une panne,
    et le recensement doit se faire quand meme."""
    routeur.raise_on_pppoe_servers = RuntimeError("no such command")
    collecteur = MikrotikCollector(settings.routers[0], client=routeur)

    vues = await collecteur.collect_vlan_clients()

    assert [v.address for v in vues] == ["10.20.0.50", "10.20.0.51", "10.30.0.9"]


async def test_une_source_perdue_remonte_l_erreur_ET_ce_qui_a_ete_vu(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Jeter les observations parce qu'une table sur seize a expire punirait
    l'exploitant deux fois : il perdrait sa liste en plus de son erreur."""
    routeur.raise_on_dhcp = RuntimeError("timeout")
    collecteur = MikrotikCollector(settings.routers[0], client=routeur)

    with pytest.raises(PartialCensusError) as capture:
        await collecteur.collect_vlan_clients()

    assert "timeout" in str(capture.value)
    assert [v.address for v in capture.value.sightings] == ["10.20.0.50", "10.30.0.9"]


async def test_le_diagnostic_porte_le_recensement_et_le_detail_arp(
    settings: Settings, routeur: FakeRouterOsClient
) -> None:
    """Les deux questions sont differentes : "pourquoi cette ligne d'ARP a-t-elle
    ete ecartee ?" et "qui vit sur ce PoP ?". Une seule lecture, deux reponses."""
    collecteur = MikrotikCollector(settings.routers[0], client=routeur)

    rapport = await collecteur.explain_vlan_clients()

    assert rapport["kept"] == 1
    assert "10.20.0.0/24" in rapport["reseaux_clients"]
    assert rapport["recensement"]["counts"]["clients"] == 3


# =========================================================================
# 7. L'API : le PoP, et ce que l'inventaire ignore
# =========================================================================


@pytest.fixture
def api(settings: Settings, routeur: FakeRouterOsClient):
    inventaire = InventaireApi()
    container = build_container(settings, static_clients_repo=inventaire, client=routeur)
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), inventaire


def test_api_recense_le_pop_et_compte_les_non_declares(api) -> None:
    """LE CHIFFRE QUI COMPTE : ce que le PoP porte et que l'inventaire ignore."""
    client, _ = api

    corps = client.get("/api/v1/pops/census").json()

    pop = corps["pops"][0]
    assert pop["pop_name"] == "PoP Test"
    adresses = [c["address"] for c in pop["clients"]]
    assert "10.20.0.50" in adresses and "10.20.0.51" in adresses
    assert pop["counts"]["non_declares"] >= 2


def test_api_rapproche_un_client_declare_par_contenance(api) -> None:
    """Un client declare en /29 est reconnu quand n'importe laquelle de ses
    adresses parle -- exactement comme le <<= cote base."""
    client, _ = api
    client.post(
        "/api/v1/static-clients",
        json={
            "reference": "boulangerie",
            "pop_name": "PoP Test",
            "address": "10.20.0.48/29",
            "plan_down_mbps": 100,
            "plan_up_mbps": 20,
        },
    )

    pop = client.get("/api/v1/pops/census").json()["pops"][0]
    declare = {c["address"]: c["declared"] for c in pop["clients"]}

    assert declare["10.20.0.50"]["reference"] == "boulangerie"
    assert declare["10.20.0.51"]["reference"] == "boulangerie"


def test_api_rend_les_remarques_du_recensement(api) -> None:
    """Sans elles, une liste courte se lirait comme un PoP vide."""
    client, _ = api
    pop = client.get("/api/v1/pops/census").json()["pops"][0]
    assert pop["remarks"]


def test_api_dit_ou_chercher_quand_aucun_routeur_ne_correspond(api) -> None:
    client, _ = api
    reponse = client.get("/api/v1/pops/census?pop_name=fantome")
    assert reponse.status_code == 404
    assert "Devices" in reponse.json()["detail"]


def test_api_n_expose_aucune_route_d_ecriture_sur_le_recensement(api) -> None:
    """Recenser est une LECTURE. Declarer reste un geste humain, ailleurs."""
    client, _ = api
    chemins = client.app.openapi()["paths"]
    assert set(chemins["/api/v1/pops/census"]) == {"get"}
