"""Identite des routeurs par leur loopback, et hierarchie de l'arbre.

DEUX CHOSES RENDAIENT L'ARBRE IRREALISTE, et ce fichier les verrouille toutes
les deux :

1. tout routeur gere etait pose en ``KIND_POP``. Le role declare dans
   l'inventaire (passerelle / coeur / PoP) n'atteignait jamais le graphe, donc
   la hierarchie s'aplatissait et la racine devenait arbitraire ;
2. l'identite d'un routeur se devinait a partir de sa MAC, de ses adresses
   d'interface ou de son nom. Or une adresse d'interface est PARTAGEE (un /30
   appartient aux deux bouts) et souvent DUPLIQUEE d'un site a l'autre par les
   configurations modeles. Le loopback, lui, est unique par construction.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.collectors.topology import (
    KIND_CORE,
    KIND_GATEWAY,
    KIND_POP,
    TopologyNode,
    TopologySnapshot,
    kind_for_role,
    loopback_from_addresses,
    neighbor_addresses,
    parse_export,
    pick_loopback,
    resolve_to_managed,
)
from app.config import RouterConfig, Settings
from app.services.registry import RouterRegistry
from app.services.shaping import ShapingService
from tests.conftest import FakeRouterOsClient

# =========================================================================
# 1. Trouver le loopback
# =========================================================================

ADRESSES = [
    {"address": "10.0.12.1/30", "interface": "ether1"},
    {"address": "192.168.88.1/24", "interface": "bridge"},
    {"address": "10.255.0.7/32", "interface": "lo"},
]


def test_une_interface_de_loopback_est_reconnue() -> None:
    assert loopback_from_addresses(ADRESSES) == ("10.255.0.7", "interface de loopback")


@pytest.mark.parametrize("nom", ["lo", "lo0", "loopback", "loopback0", "bridge-loopback"])
def test_les_noms_usuels_de_loopback(nom: str) -> None:
    rows = [{"address": "10.255.0.7/32", "interface": nom}]
    assert loopback_from_addresses(rows) == ("10.255.0.7", "interface de loopback")


def test_une_adresse_de_liaison_n_est_jamais_un_loopback() -> None:
    """Un /30 appartient AUX DEUX bouts du lien : il ne peut identifier ni l'un
    ni l'autre. C'est la raison d'etre de tout ce fichier."""
    assert loopback_from_addresses([{"address": "10.0.12.1/30", "interface": "ether1"}]) is None


def test_un_32_isole_sert_de_repli() -> None:
    rows = [
        {"address": "10.0.12.1/30", "interface": "ether1"},
        {"address": "10.255.0.8/32", "interface": "bridge-core"},
    ]
    assert loopback_from_addresses(rows) == ("10.255.0.8", "adresse en /32")


def test_une_interface_desactivee_est_ignoree() -> None:
    rows = [{"address": "10.255.0.7/32", "interface": "lo", "disabled": "true"}]
    assert loopback_from_addresses(rows) is None


def test_le_choix_est_deterministe() -> None:
    """Deux loopbacks sur un meme routeur : le resultat ne doit pas dependre de
    l'ordre de lecture, sinon l'identite change d'un cycle a l'autre."""
    a = [
        {"address": "10.255.0.9/32", "interface": "lo"},
        {"address": "10.255.0.7/32", "interface": "lo"},
    ]
    assert loopback_from_addresses(a)[0] == loopback_from_addresses(list(reversed(a)))[0]


def test_la_declaration_bat_toute_deduction() -> None:
    """Convention de tout le depot : ce que l'operateur a saisi fait foi."""
    assert pick_loopback(declared="10.255.0.99", addresses=ADRESSES) == ("10.255.0.99", "declare")


def test_le_router_id_sert_quand_aucune_interface_ne_s_appelle_lo() -> None:
    """Dans un reseau d'operateur, le router-id EST le loopback."""
    rows = [
        {"address": "10.0.12.1/30", "interface": "ether1"},
        {"address": "10.255.0.8/32", "interface": "bridge-core"},
    ]
    assert pick_loopback(declared=None, addresses=rows, router_id="10.255.0.8") == (
        "10.255.0.8",
        "router-id",
    )


def test_l_interface_lo_bat_le_router_id() -> None:
    """Un router-id peut etre fige a la main sur une valeur historique ; une
    interface nommee 'lo' est une declaration d'intention plus fiable."""
    assert pick_loopback(declared=None, addresses=ADRESSES, router_id="10.255.0.8") == (
        "10.255.0.7",
        "interface de loopback",
    )


def test_aucun_loopback_est_dit_explicitement() -> None:
    rows = [{"address": "10.0.12.1/30", "interface": "ether1"}]
    assert pick_loopback(declared=None, addresses=rows) == (None, "introuvable")


@pytest.mark.parametrize("mauvais", ["10.255.0.0/24", "0.0.0.0", "127.0.0.1", "pas-une-ip", ""])
def test_un_loopback_invalide_est_refuse_a_la_saisie(mauvais: str) -> None:
    """Echouer au chargement plutot qu'a la reconciliation : une identite fausse
    est pire qu'une identite absente."""
    if mauvais == "":
        assert _config(loopback=mauvais).loopback is None
        return
    with pytest.raises(ValidationError):
        _config(loopback=mauvais)


def test_le_loopback_est_normalise() -> None:
    assert _config(loopback="10.255.0.1/32").loopback == "10.255.0.1"


# =========================================================================
# 2. Le router-id dans l'export
# =========================================================================


def test_router_id_v7() -> None:
    export = "/routing id\nadd disabled=no id=10.255.0.7 name=main-id\n"
    assert parse_export(export)["router_ids"] == ["10.255.0.7"]


def test_router_id_ospf_et_bgp() -> None:
    export = (
        "/routing ospf instance\nadd name=default router-id=10.255.0.7\n"
        "/routing bgp instance\nset default router-id=10.255.0.7\n"
    )
    # Deux instances, une seule valeur : pas de doublon.
    assert parse_export(export)["router_ids"] == ["10.255.0.7"]


def test_un_export_sans_routage_ne_donne_rien() -> None:
    assert (
        parse_export("/ip address\nadd address=10.0.0.1/30 interface=ether1\n")["router_ids"] == []
    )


# =========================================================================
# 3. Les adresses annoncees par un voisin
# =========================================================================


def test_toutes_les_adresses_annoncees_sont_gardees() -> None:
    """MNDP annonce l'adresse de liaison ET, selon la version, les autres. Le
    loopback est dans la seconde liste : n'en garder qu'une revient a jeter la
    seule adresse qui prouve quelque chose."""
    voisin = {"address": "10.0.1.1", "unicast-ipv4-addresses": "10.0.1.1,10.255.0.2"}
    assert neighbor_addresses(voisin) == ["10.0.1.1", "10.255.0.2"]


def test_les_prefixes_et_doublons_sont_nettoyes() -> None:
    voisin = {"address": "10.0.1.1/30", "ipv4-addresses": "10.0.1.1, 10.255.0.2/32"}
    assert neighbor_addresses(voisin) == ["10.0.1.1", "10.255.0.2"]


def test_un_voisin_muet_ne_casse_rien() -> None:
    assert neighbor_addresses({"identity": "switch"}) == []


# =========================================================================
# 4. Le role declare donne sa hierarchie a l'arbre
# =========================================================================


@pytest.mark.parametrize(
    ("role", "attendu"),
    [
        ("gateway", KIND_GATEWAY),
        ("core", KIND_CORE),
        ("pop", KIND_POP),
        ("GATEWAY", KIND_GATEWAY),
        (None, KIND_POP),
        ("inconnu", KIND_POP),
    ],
)
def test_correspondance_role_nature(role: str | None, attendu: str) -> None:
    assert kind_for_role(role) == attendu


# =========================================================================
# 5. La reconciliation : le loopback tranche seul
# =========================================================================


def _snapshot_avec_deux_routeurs() -> TopologySnapshot:
    snapshot = TopologySnapshot()
    snapshot.add_node(TopologyNode(key="router:a", name="A", kind=KIND_CORE))
    snapshot.add_node(TopologyNode(key="router:b", name="B", kind=KIND_POP))
    return snapshot


def test_le_loopback_bat_l_adresse_d_interface() -> None:
    """LE test qui compte.

    Deux sites deployes avec la meme configuration modele portent le meme /30.
    L'index des adresses designe alors le mauvais routeur -- et le lien du coeur
    aboutit sur le mauvais PoP. Le loopback, unique, corrige sans ambiguite.
    """
    snapshot = _snapshot_avec_deux_routeurs()
    snapshot.add_node(
        TopologyNode(
            key="mac:CC:CC:CC:CC:CC:CC",
            name="vu-par-le-coeur",
            attributes={"addresses": ["10.0.0.1", "10.255.0.11"]},
        )
    )

    replies = resolve_to_managed(
        snapshot,
        ip_owner={"10.0.0.1": "router:a"},  # ambigu : deux sites l'ont
        mac_owner={},
        name_owner={},
        loopback_owner={"10.255.0.11": "router:b"},  # sans ambiguite
    )

    assert replies == 1
    assert "mac:CC:CC:CC:CC:CC:CC" not in snapshot.nodes
    assert "router:b" in snapshot.nodes


def test_le_loopback_bat_la_mac() -> None:
    """Une MAC depend du port par lequel on regarde l'equipement ; elle peut
    aussi etre reprise sur un materiel remplace."""
    snapshot = _snapshot_avec_deux_routeurs()
    snapshot.add_node(
        TopologyNode(
            key="mac:DD:DD:DD:DD:DD:DD",
            name="vu",
            mac="DD:DD:DD:DD:DD:DD",
            attributes={"addresses": ["10.255.0.11"]},
        )
    )

    resolve_to_managed(
        snapshot,
        ip_owner={},
        mac_owner={"DD:DD:DD:DD:DD:DD": "router:a"},
        name_owner={},
        loopback_owner={"10.255.0.11": "router:b"},
    )

    assert "router:b" in snapshot.nodes
    assert "mac:DD:DD:DD:DD:DD:DD" not in snapshot.nodes


def test_sans_loopback_les_anciens_criteres_restent() -> None:
    """Les voisins NON GERES n'ont pas de loopback connu : la MAC et le nom
    doivent continuer a servir, sinon on regresserait sur tout le reste."""
    snapshot = _snapshot_avec_deux_routeurs()
    snapshot.add_node(TopologyNode(key="mac:EE:EE:EE:EE:EE:EE", name="vu", mac="EE:EE:EE:EE:EE:EE"))

    replies = resolve_to_managed(
        snapshot,
        ip_owner={},
        mac_owner={"EE:EE:EE:EE:EE:EE": "router:a"},
        name_owner={},
        loopback_owner={},
    )

    assert replies == 1


# =========================================================================
# 6. De bout en bout : un vrai reseau d'operateur
# =========================================================================


def _config(**kwargs) -> RouterConfig:
    base = {
        "name": "r",
        "host": "10.0.0.1",
        "username": "u",
        "password": SecretStr("p"),
    }
    return RouterConfig(**{**base, **kwargs})


def _routeur(identite: str, loopback: str, voisins: list[dict], liaison: str) -> FakeRouterOsClient:
    client = FakeRouterOsClient(identity=identite)
    client.address_rows = [
        {"address": f"{loopback}/32", "interface": "lo"},
        {"address": liaison, "interface": "ether1"},
    ]
    client.neighbor_rows = voisins
    client.ethernet_rows = [
        {"name": "ether1", "speed": "10Gbps"},
        {"name": "ether2", "speed": "10Gbps"},
    ]
    return client


def _service(clients: dict[str, FakeRouterOsClient], configs: list[RouterConfig]) -> ShapingService:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x/y",
        routers=configs,
        backhaul_provider="mock",
        plan_provider="mock",
        scheduler_enabled=False,
    )
    registry = RouterRegistry(settings, client_factory=lambda cfg: clients[cfg.name])
    return ShapingService(settings, registry=registry)


async def test_l_arbre_a_une_vraie_hierarchie() -> None:
    """Passerelle, coeur et PoPs a leur place -- et pas trois PoPs.

    C'est ce rang qui decide de la racine de l'arbre et de la profondeur des
    branches cote interface. Tout poser en PoP l'aplatissait.
    """
    clients = {
        "gw": _routeur("gw", "10.255.0.1", [], "10.0.0.1/30"),
        "core": _routeur("core", "10.255.0.2", [], "10.0.0.2/30"),
        "pop": _routeur("pop", "10.255.0.10", [], "10.0.1.2/30"),
    }
    configs = [
        _config(name="gw", host="1.1.1.1", role="gateway", pop_name="Paris"),
        _config(name="core", host="1.1.1.2", role="core", pop_name="Coeur"),
        _config(name="pop", host="1.1.1.3", role="pop", pop_name="PoP Nord"),
    ]
    service = _service(clients, configs)
    await service.registry.reload()

    snapshot = await service.discover()

    natures = {n.name: n.kind for n in snapshot.nodes.values()}
    assert natures == {"Paris": KIND_GATEWAY, "Coeur": KIND_CORE, "PoP Nord": KIND_POP}
    # Et chacun porte son loopback, avec l'origine de la deduction.
    for noeud in snapshot.nodes.values():
        assert noeud.attributes["loopback_source"] == "interface de loopback"


async def test_deux_sites_au_meme_30_ne_se_confondent_plus() -> None:
    """REGRESSION.

    Configurations modeles : chaque PoP porte 10.0.0.1/30 vers son acces. Sans
    le loopback, les deux liens du coeur aboutissaient sur le MEME PoP et
    l'autre restait orphelin -- un arbre qui montre un reseau qui n'existe pas.
    """

    def pop(nom: str, loopback: str) -> FakeRouterOsClient:
        client = FakeRouterOsClient(identity=nom)
        client.address_rows = [
            {"address": f"{loopback}/32", "interface": "lo"},
            {"address": "10.0.0.1/30", "interface": "ether1"},  # le MEME partout
        ]
        client.ethernet_rows = [{"name": "ether1", "speed": "1Gbps"}]
        return client

    coeur = FakeRouterOsClient(identity="core")
    coeur.address_rows = [{"address": "10.255.0.2/32", "interface": "lo"}]
    coeur.ethernet_rows = [
        {"name": "ether1", "speed": "10Gbps"},
        {"name": "ether2", "speed": "10Gbps"},
    ]
    coeur.neighbor_rows = [
        {
            "interface": "ether1",
            "identity": "pop-nord",
            "mac-address": "AA:00:00:00:00:10",
            "platform": "MikroTik",
            "unicast-ipv4-addresses": "10.0.0.1,10.255.0.10",
        },
        {
            "interface": "ether2",
            "identity": "pop-sud",
            "mac-address": "AA:00:00:00:00:11",
            "platform": "MikroTik",
            "unicast-ipv4-addresses": "10.0.0.1,10.255.0.11",
        },
    ]
    clients = {
        "core": coeur,
        "pop-nord": pop("pop-nord", "10.255.0.10"),
        "pop-sud": pop("pop-sud", "10.255.0.11"),
    }
    configs = [
        _config(name="core", host="1.1.1.2", role="core", pop_name="Coeur"),
        _config(name="pop-nord", host="1.1.1.10", role="pop", pop_name="PoP Nord"),
        _config(name="pop-sud", host="1.1.1.11", role="pop", pop_name="PoP Sud"),
    ]
    service = _service(clients, configs)
    await service.registry.reload()

    snapshot = await service.discover()

    # Trois routeurs, trois cases : aucun doublon, aucun disparu.
    assert len(snapshot.nodes) == 3
    depuis_coeur = {
        lien.interface: snapshot.nodes[lien.target_key].name
        for lien in snapshot.links.values()
        if snapshot.nodes[lien.source_key].name == "Coeur"
    }
    assert depuis_coeur == {"ether1": "PoP Nord", "ether2": "PoP Sud"}


async def test_un_loopback_partage_est_refuse_et_signale() -> None:
    """L'unicite est la PROMESSE du modele, donc c'est ce qu'il faut verifier.

    Deux routeurs qui la violent sont une erreur de configuration. Les fusionner
    silencieusement donnerait un arbre faux ; on ecarte donc l'adresse de
    l'index et on le dit fort.
    """
    clients = {
        "a": _routeur("a", "10.255.0.5", [], "10.0.0.1/30"),
        "b": _routeur("b", "10.255.0.5", [], "10.0.1.1/30"),
    }
    configs = [
        _config(name="a", host="1.1.1.1", role="pop", pop_name="A"),
        _config(name="b", host="1.1.1.2", role="pop", pop_name="B"),
    ]
    service = _service(clients, configs)
    await service.registry.reload()

    snapshot = await service.discover()

    assert len(snapshot.nodes) == 2
    assert any("doit etre unique" in a for a in snapshot.warnings)
    assert any("10.255.0.5" in a for a in snapshot.warnings)


async def test_un_routeur_sans_loopback_est_signale() -> None:
    """Silencieusement retomber sur des criteres moins surs serait pire que le
    dire : l'operateur doit savoir pourquoi son arbre est approximatif."""
    client = FakeRouterOsClient(identity="sans-lo")
    client.address_rows = [{"address": "10.0.0.1/30", "interface": "ether1"}]
    client.ethernet_rows = [{"name": "ether1", "speed": "1Gbps"}]
    service = _service(
        {"sans-lo": client},
        [_config(name="sans-lo", host="1.1.1.1", role="pop", pop_name="Sans")],
    )
    await service.registry.reload()

    snapshot = await service.discover()

    assert any("aucun loopback trouve" in a for a in snapshot.warnings)


async def test_un_routeur_injoignable_garde_son_role_et_son_loopback() -> None:
    """Sinon un coeur en panne se retrouverait pose en PoP, et l'arbre se
    reorganiserait autour d'une panne."""
    client = FakeRouterOsClient(identity="core")
    client.raise_on_neighbors = RuntimeError("injoignable")
    service = _service(
        {"core": client},
        [
            _config(
                name="core", host="1.1.1.2", role="core", pop_name="Coeur", loopback="10.255.0.2"
            )
        ],
    )
    await service.registry.reload()

    snapshot = await service.discover()

    noeud = snapshot.nodes["router:core"]
    assert noeud.kind == KIND_CORE
    assert noeud.attributes["unreachable"] is True
    assert noeud.attributes["loopback"] == "10.255.0.2"
