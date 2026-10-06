"""L'arbre reseau ne contient QUE ce que l'exploitant a declare.

Routeurs ajoutes (Devices ou API), sites rattaches a un routeur, et les clients
vus sous eux (accroches par l'interface). Ni voisin MNDP, ni radio decouverte,
ni switch, ni lien deduit.
"""

from __future__ import annotations

from app.services.declared_tree import build_declared_tree

ROUTEURS = [
    {"name": "gw", "pop_name": "Gateway", "role": "gateway", "host": "10.0.0.1"},
    {"name": "core", "pop_name": "Core", "role": "core", "host": "10.0.0.2"},
    {"name": "nas-a", "pop_name": "PoP A", "role": "pop", "host": "10.0.1.1"},
    {"name": "nas-b", "pop_name": "PoP B", "role": "pop", "host": "10.0.2.1"},
]


def _parents(arbre: dict) -> dict[str, str]:
    return {lien["target_key"]: lien["source_key"] for lien in arbre["links"]}


def test_seuls_les_routeurs_ajoutes_et_leurs_sites_entrent_dans_l_arbre() -> None:
    decouverts = [
        {"key": "router:nas-a", "kind": "pop", "pos_x": 10.0, "pos_y": 20.0},
        {"key": "mac:AA:BB", "name": "switch-gestion", "kind": "unknown"},
        {"key": "uisp:radio-1", "name": "AP Nord", "kind": "radio"},
    ]
    sites = [
        {
            "name": "Francophonie",
            "kind": "vlan",
            "router_name": "nas-a",
            "vlan_id": 2060,
            "vlan_interface": "vlan2060",
        },
        {"name": "PoP A", "kind": "router", "router_name": "nas-a"},
        {"name": "Orphelin", "kind": "vlan", "router_name": "inconnu"},
    ]
    arbre = build_declared_tree(ROUTEURS, known_nodes=decouverts, sites=sites)
    cles = {n["key"] for n in arbre["nodes"]}
    assert cles == {
        "router:gw",
        "router:core",
        "router:nas-a",
        "router:nas-b",
        "site:Francophonie",
    }
    # La position posee a la main est conservee.
    nas_a = next(n for n in arbre["nodes"] if n["key"] == "router:nas-a")
    assert (nas_a["pos_x"], nas_a["pos_y"]) == (10.0, 20.0)
    assert nas_a["name"] == "PoP A"  # les clients s'y accrochent par ce nom


def test_la_hierarchie_vient_du_role_declare() -> None:
    parents = _parents(build_declared_tree(ROUTEURS))
    assert parents["router:core"] == "router:gw"
    assert parents["router:nas-a"] == "router:core"
    assert parents["router:nas-b"] == "router:core"
    assert "router:gw" not in parents


def test_deux_coeurs_le_pop_reste_a_la_racine_plutot_que_deviner() -> None:
    routeurs = [*ROUTEURS, {"name": "core2", "role": "core"}]
    parents = _parents(build_declared_tree(routeurs))
    assert "router:nas-a" not in parents
    assert parents["router:core2"] == "router:gw"


def test_le_parent_force_a_la_main_est_transmis() -> None:
    connus = [{"key": "router:nas-b", "parent_override": "router:nas-a"}]
    arbre = build_declared_tree(ROUTEURS, known_nodes=connus)
    nas_b = next(n for n in arbre["nodes"] if n["key"] == "router:nas-b")
    assert nas_b["parent_override"] == "router:nas-a"
    assert all(n["config_parent"] is None for n in arbre["nodes"])


def test_le_lien_vers_un_site_de_vlan_porte_le_debit_de_son_port() -> None:
    sites = [
        {
            "name": "Francophonie",
            "kind": "vlan",
            "router_name": "nas-a",
            "vlan_interface": "vlan2060",
        }
    ]
    mesures = [
        {
            "router_name": "nas-a",
            "interface": "vlan2060",
            "rx_bps": 1e6,
            "tx_bps": 9e6,
            "running": True,
            "fresh": True,
        }
    ]
    arbre = build_declared_tree(ROUTEURS, sites=sites, interfaces=mesures)
    lien = next(lk for lk in arbre["links"] if lk["target_key"] == "site:Francophonie")
    assert lien["source_key"] == "router:nas-a"
    assert lien["tx_bps"] == 9e6 and lien["interface"] == "vlan2060"
    assert lien["declared"] is True and lien["key"].startswith("declared:")
