"""Le CPE d'un abonne ne doit pas faire une case de plus dans l'arbre.

Le routeur d'un abonne arrive par deux chemins : une session ``/ppp/active``
-- c'est l'abonne, avec son login -- et un voisin ``/ip/neighbor`` au bout du
port du PoP -- c'est un equipement decouvert. Sans jointure, l'arbre montrait
les deux et l'exploitant y comptait plus de clients qu'il n'en a.
"""

from __future__ import annotations

from typing import Any

from app.collectors.topology import (
    KIND_CPE,
    KIND_POP,
    LINK_ETHERNET,
    TopologyLink,
    TopologyNode,
    TopologySnapshot,
    mac_from_link_local,
    mark_subscriber_cpes,
)


def _graphe() -> TopologySnapshot:
    snapshot = TopologySnapshot()
    snapshot.add_node(
        TopologyNode(
            key="router:nas-bassora",
            name="BASSORA",
            kind=KIND_POP,
            mac="50:00:00:01:00:00",
            attributes={"managed": True, "macs": ["50:00:00:01:00:00"]},
        )
    )
    return snapshot


def test_une_mac_se_relit_dans_une_adresse_de_lien_local() -> None:
    """RFC 4291 : le lien-local EUI-64 EST la MAC, a deux bits pres."""
    assert mac_from_link_local("fe80::5200:ff:fe09:0") == "50:00:00:09:00:00"
    assert mac_from_link_local("fe80::5200:00ff:fe09:0000/64") == "50:00:00:09:00:00"
    assert mac_from_link_local("fe80::5200:ff:fe09:0%ether1") == "50:00:00:09:00:00"


def test_une_adresse_qui_ne_porte_pas_de_mac_n_en_invente_pas() -> None:
    assert mac_from_link_local("fe80::1") is None  # identifiant manuel
    assert mac_from_link_local("2001:db8::5200:ff:fe09:0") is None  # pas un lien-local
    assert mac_from_link_local("192.168.88.1") is None
    assert mac_from_link_local("") is None
    assert mac_from_link_local(None) is None


def test_le_voisin_dont_la_mac_est_le_caller_id_est_le_cpe_de_l_abonne() -> None:
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(
            key="mac:50:00:00:09:00:00",
            name="client3",
            kind=KIND_POP,  # la banniere MNDP disait 'MikroTik'
            mac="50:00:00:09:00:00",
        )
    )

    marques = mark_subscriber_cpes(snapshot, {"50:00:00:09:00:00": "test-ba"})

    assert marques == 1
    cpe = snapshot.nodes["mac:50:00:00:09:00:00"]
    assert cpe.attributes["subscriber_cpe"] == "test-ba"
    assert cpe.kind == KIND_CPE


def test_un_cpe_qui_n_annonce_qu_une_adresse_de_lien_local_est_reconnu() -> None:
    """Le cas exact du labo : le voisin n'a pas d'IPv4 sur ce segment."""
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(
            key="identity:MikroTik",
            name="MikroTik",
            kind=KIND_POP,
            mac=None,
            attributes={"addresses": ["fe80::5200:ff:fe08:0"]},
        )
    )

    assert mark_subscriber_cpes(snapshot, {"50:00:00:08:00:00": "test-ta"}) == 1
    assert snapshot.nodes["identity:MikroTik"].attributes["subscriber_cpe"] == "test-ta"


def test_un_routeur_de_l_inventaire_n_est_jamais_reclasse_en_cpe() -> None:
    """Un PoP qui ouvre lui-meme une session PPPoE vers son transit.

    Le reclasser le ferait disparaitre de l'arbre avec tout ce qui pend dessous.
    """
    snapshot = _graphe()

    assert mark_subscriber_cpes(snapshot, {"50:00:00:01:00:00": "transit-bassora"}) == 0
    gere = snapshot.nodes["router:nas-bassora"]
    assert "subscriber_cpe" not in gere.attributes
    assert gere.kind == KIND_POP


def test_un_voisin_sans_session_correspondante_reste_intact() -> None:
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(key="mac:aa:bb:cc:dd:ee:ff", name="switch", mac="aa:bb:cc:dd:ee:ff")
    )

    assert mark_subscriber_cpes(snapshot, {"50:00:00:09:00:00": "test-ba"}) == 0
    assert "subscriber_cpe" not in snapshot.nodes["mac:aa:bb:cc:dd:ee:ff"].attributes


def test_aucune_session_ne_marque_rien() -> None:
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(key="mac:50:00:00:09:00:00", name="client3", mac="50:00:00:09:00:00")
    )

    assert mark_subscriber_cpes(snapshot, {}) == 0
    assert mark_subscriber_cpes(snapshot, {"": "sans-mac"}) == 0


def test_le_lien_vers_le_cpe_reste_dans_le_graphe() -> None:
    """Le marquage n'est pas une suppression : le rattachement en depend."""
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(key="mac:50:00:00:09:00:00", name="client3", mac="50:00:00:09:00:00")
    )
    snapshot.add_link(
        TopologyLink(
            source_key="router:nas-bassora",
            target_key="mac:50:00:00:09:00:00",
            kind=LINK_ETHERNET,
            interface="ether2",
        )
    )

    mark_subscriber_cpes(snapshot, {"50:00:00:09:00:00": "test-ba"})

    assert len(snapshot.links) == 1
    assert "mac:50:00:00:09:00:00" in snapshot.nodes


def test_un_cpe_deja_marque_n_est_pas_recompte() -> None:
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(key="mac:50:00:00:09:00:00", name="client3", mac="50:00:00:09:00:00")
    )
    caller_ids: dict[str, str] = {"50:00:00:09:00:00": "test-ba"}

    assert mark_subscriber_cpes(snapshot, caller_ids) == 1
    assert mark_subscriber_cpes(snapshot, caller_ids) == 0


def test_l_api_ne_sert_pas_la_case_d_un_cpe_d_abonne() -> None:
    """Meme regle que pour un client a IP fixe : une seule case par client."""
    from app.api.shaping import _attributs

    noeuds: list[dict[str, Any]] = [
        {"key": "router:nas-bassora", "kind": KIND_POP, "attributes": {"managed": True}},
        {
            "key": "mac:50:00:00:09:00:00",
            "kind": KIND_CPE,
            "attributes": {"subscriber_cpe": "test-ba"},
        },
        # Meme chose, mais relue depuis la base : les attributs y sont du JSON brut.
        {
            "key": "mac:50:00:00:08:00:00",
            "kind": KIND_CPE,
            "attributes": '{"subscriber_cpe": "test-ta"}',
        },
    ]

    caches = {n["key"] for n in noeuds if _attributs(n).get("subscriber_cpe")}

    assert caches == {"mac:50:00:00:09:00:00", "mac:50:00:00:08:00:00"}


# =========================================================================
# Bout en bout : le labo tel qu'il est, un PoP et le routeur d'un abonne
# =========================================================================


def _client_pop() -> Any:
    """Un PoP qui voit le routeur de son abonne au bout d'ether2.

    Le voisin n'annonce que son lien-local -- il n'a pas d'IPv4 sur ce segment,
    exactement ce que montre le labo -- et sa plateforme dit 'MikroTik'.
    """
    from tests.conftest import FakeRouterOsClient

    client = FakeRouterOsClient(identity="NAS-BASSORA")
    client.active = [
        {
            "name": "test-ba",
            "caller-id": "50:00:00:09:00:00",
            "address": "172.16.25.253",
            "service": "pppoe",
        }
    ]
    client.address_rows = [
        {"address": "10.255.0.10/32", "interface": "lo"},
        {"address": "172.16.25.1/24", "interface": "ether2"},
    ]
    client.neighbor_rows = [
        {
            "interface": "ether2",
            "identity": "client3",
            "platform": "MikroTik",
            "address6": "fe80::5200:ff:fe09:0",
        }
    ]
    client.ethernet_rows = [{"name": "ether2", "speed": "1Gbps"}]
    return client


async def test_le_routeur_d_un_abonne_ne_fait_pas_une_case_de_plus() -> None:
    from pydantic import SecretStr

    from app.config import RouterConfig, Settings
    from app.services.registry import RouterRegistry
    from app.services.shaping import ShapingService

    client = _client_pop()
    config = RouterConfig(
        name="nas-bassora",
        host="10.0.0.10",
        username="u",
        password=SecretStr("p"),
        role="pop",
        pop_name="BASSORA",
    )
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=[config],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    service = ShapingService(
        settings, registry=RouterRegistry(settings, client_factory=lambda _: client)
    )
    await service.registry.reload()

    snapshot = await service.discover()

    # Le voisin est bien la -- son lien porte le rattachement de l'abonne --
    # mais il est identifie comme le CPE de 'test-ba', pas comme un site de plus.
    cpes = [n for n in snapshot.nodes.values() if n.attributes.get("subscriber_cpe")]
    assert [n.attributes["subscriber_cpe"] for n in cpes] == ["test-ba"]
    assert cpes[0].kind == KIND_CPE
    # Il n'avait AUCUNE MAC annoncee : la jointure a relu celle de son
    # lien-local. Sans cela ce voisin restait une case anonyme a cote de
    # l'abonne qu'il est.
    assert cpes[0].mac is None
    assert cpes[0].key == "identity:client3"

    # Et surtout : un seul PoP dans le graphe, celui de l'inventaire.
    pops = [n.name for n in snapshot.nodes.values() if n.kind == KIND_POP]
    assert pops == ["BASSORA"]


# =========================================================================
# Le client sur VLAN routee : pas de session, donc pas de caller-id
# =========================================================================


def test_le_voisin_qui_annonce_l_adresse_d_un_client_declare_est_son_cpe() -> None:
    """Le cas Francophonie : un client par VLAN, declare par son adresse.

    Sans session PPPoE il n'y a aucun 'caller-id' a joindre. L'adresse declaree
    est alors la seule egalite disponible -- et elle suffit.
    """
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(
            key="identity:MikroTik",
            name="MikroTik",
            attributes={"addresses": ["172.16.35.253", "fe80::5200:ff:fe0a:0"]},
        )
    )

    marques = mark_subscriber_cpes(snapshot, {}, {"172.16.35.253": "test-fp"})

    assert marques == 1
    assert snapshot.nodes["identity:MikroTik"].attributes["subscriber_cpe"] == "test-fp"


def test_l_adresse_ne_rattrape_pas_un_voisin_d_un_autre_segment() -> None:
    snapshot = _graphe()
    snapshot.add_node(TopologyNode(key="mac:aa:bb:cc:00:00:01", name="AP", address="172.16.35.2"))

    assert mark_subscriber_cpes(snapshot, {}, {"172.16.35.253": "test-fp"}) == 0


def test_la_mac_prime_sur_l_adresse() -> None:
    """Deux preuves qui se contredisent : on garde la plus forte.

    Une adresse peut etre reattribuee, une MAC de CPE non.
    """
    snapshot = _graphe()
    snapshot.add_node(
        TopologyNode(
            key="mac:50:00:00:09:00:00",
            name="client3",
            mac="50:00:00:09:00:00",
            address="172.16.35.253",
        )
    )

    mark_subscriber_cpes(snapshot, {"50:00:00:09:00:00": "test-ba"}, {"172.16.35.253": "test-fp"})

    assert snapshot.nodes["mac:50:00:00:09:00:00"].attributes["subscriber_cpe"] == "test-ba"


def test_un_index_d_adresses_vide_ne_marque_rien() -> None:
    snapshot = _graphe()
    snapshot.add_node(TopologyNode(key="identity:x", name="x", address="172.16.35.253"))

    assert mark_subscriber_cpes(snapshot, {}, {}) == 0
    assert mark_subscriber_cpes(snapshot, {}, {"": "sans-adresse"}) == 0
    assert mark_subscriber_cpes(snapshot, {}, {"172.16.35.253": ""}) == 0


async def test_le_routeur_d_un_client_vlan_ne_fait_pas_une_case_de_plus() -> None:
    """Bout en bout, le cas Francophonie : un client declare par VLAN."""
    from pydantic import SecretStr

    from app.config import RouterConfig, Settings
    from app.services.registry import RouterRegistry
    from app.services.shaping import ShapingService
    from tests.conftest import FakeRouterOsClient
    from tests.test_clients_statiques import InventaireMemoire, fiche

    client = FakeRouterOsClient(identity="NAS-FRANCOPHONIE")
    client.address_rows = [
        {"address": "10.255.0.35/32", "interface": "lo"},
        {"address": "172.16.35.1/24", "interface": "vlan35"},
    ]
    client.neighbor_rows = [
        {
            "interface": "vlan35",
            "identity": "MikroTik",
            "platform": "MikroTik",
            "address": "172.16.35.253",
        }
    ]

    config = RouterConfig(
        name="nas-francophonie",
        host="10.0.0.35",
        username="u",
        password=SecretStr("p"),
        role="pop",
        pop_name="FRANCOPHONIE",
    )
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=[config],
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    service = ShapingService(
        settings, registry=RouterRegistry(settings, client_factory=lambda _: client)
    )
    service.static_clients = InventaireMemoire(
        [fiche(reference="test-fp", pop_name="FRANCOPHONIE", address="172.16.35.253", vlan=35)]
    )
    await service.registry.reload()

    snapshot = await service.discover()

    cpes = [n for n in snapshot.nodes.values() if n.attributes.get("subscriber_cpe")]
    assert [n.attributes["subscriber_cpe"] for n in cpes] == ["test-fp"]
    pops = [n.name for n in snapshot.nodes.values() if n.kind == KIND_POP]
    assert pops == ["FRANCOPHONIE"]
