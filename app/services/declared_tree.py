"""L'arbre reseau tel que l'exploitant l'a DECLARE, sans rien deviner.

POURQUOI
--------
L'arbre etait construit par decouverte : voisins MNDP/LLDP, sous-reseaux /30,
routes par defaut, radios UISP, VLAN vus sur les ports. Il montrait donc tout
ce que les routeurs voient -- imprimantes de gestion, switches, equipements
d'autres operateurs -- et se reorganisait a chaque passage. L'exploitant veut
l'inverse : un arbre qui ne contient QUE ce qu'il a ajoute.

CE QUI ENTRE DANS L'ARBRE
-------------------------
1. Les ROUTEURS ajoutes (onglet Devices ou API), avec leur role declare.
2. Les SITES rattaches a un routeur (site de VLAN, site pousse par l'API) :
   ils pendent du routeur qui les dessert.
3. Les CLIENTS vus (sessions PPPoE, clients a IP fixe declares) : ils sont
   accroches par l'interface a leur site ou a leur routeur, comme avant.

LA HIERARCHIE DES ROUTEURS VIENT DE LEUR ROLE
---------------------------------------------
passerelle > coeur > PoP. Un routeur pend du SEUL routeur du rang juste
au-dessus ; s'il y en a plusieurs, rien ne permet de choisir sans deviner :
il reste a la racine, et l'exploitant le range d'un glisser-deposer (le parent
force a la main prime toujours).

Ce que la lecture des routeurs apprend par ailleurs (loopback de la sonde,
vitesse des ports, files parentes du shaping) continue de servir la ou il
sert ; il n'apparait simplement plus dans l'arbre.

Fonction PURE : des listes en entree, des cases et des liens en sortie.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.collectors.topology import kind_for_role, router_node_key

#: Rang de chaque nature de routeur : plus petit = plus haut dans l'arbre.
RANG_ROUTEUR = {"gateway": 0, "core": 1, "pop": 2}

#: Prefixe des liens fabriques ici : ils n'existent pas en base, et ne portent
#: donc ni surcharge de debit ni historique propre.
PREFIXE_LIEN = "declared:"


def site_node_key(name: str) -> str:
    return f"site:{name}"


def _lien(parent: str, enfant: str, **champs: Any) -> dict[str, Any]:
    return {
        "key": f"{PREFIXE_LIEN}{parent}>{enfant}",
        "source_key": parent,
        "target_key": enfant,
        "kind": "declared",
        "interface": None,
        "discovered_by": "declared",
        "declared": True,
        "attributes": {"declared": True},
        "fresh": True,
        **champs,
    }


def build_declared_tree(
    routers: Iterable[dict[str, Any]],
    *,
    known_nodes: Iterable[dict[str, Any]] = (),
    sites: Iterable[dict[str, Any]] = (),
    interfaces: Iterable[dict[str, Any]] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Cases et liens de l'arbre declare.

    ``routers`` : ``name``, ``pop_name``, ``role``, ``host``.
    ``known_nodes`` : les lignes de ``topology_nodes`` -- seules celles des
    routeurs ajoutes sont reprises, pour leur position, leur parent force a la
    main et leur etat (injoignable).
    ``sites`` : les lignes de ``pops`` (``name``, ``kind``, ``router_name``,
    ``vlan_interface``).
    ``interfaces`` : derniere mesure de chaque port, pour le debit du lien
    routeur -> site de VLAN.
    """
    deja = {str(n.get("key")): n for n in known_nodes}
    noeuds: list[dict[str, Any]] = []
    liens: list[dict[str, Any]] = []

    routeurs = sorted(routers, key=lambda r: str(r.get("name") or ""))
    par_nom: dict[str, dict[str, Any]] = {}
    for r in routeurs:
        nom = str(r["name"])
        cle = router_node_key(nom)
        ancien = deja.get(cle) or {}
        nature = kind_for_role(r.get("role"))
        noeud = {
            "key": cle,
            "name": str(r.get("pop_name") or nom),
            "kind": ancien.get("kind_override") or nature,
            "kind_detected": nature,
            "kind_override": ancien.get("kind_override"),
            "address": r.get("host"),
            "router_name": nom,
            "attributes": {
                **_attributs(ancien),
                "managed": True,
                "role": str(r.get("role") or "pop"),
            },
            "pos_x": ancien.get("pos_x"),
            "pos_y": ancien.get("pos_y"),
            "parent_override": ancien.get("parent_override"),
            "config_parent": None,
            "hidden": False,
            "fresh": ancien.get("fresh", True),
            "last_seen": ancien.get("last_seen"),
        }
        noeuds.append(noeud)
        par_nom[nom] = noeud

    # Hierarchie par role : le seul routeur du rang juste au-dessus.
    rangs = {n["key"]: RANG_ROUTEUR.get(n["kind_detected"], 2) for n in par_nom.values()}
    for noeud in par_nom.values():
        rang = rangs[noeud["key"]]
        au_dessus = [r for r in set(rangs.values()) if r < rang]
        if not au_dessus:
            continue
        cible = max(au_dessus)
        candidats = [cle for cle, r in rangs.items() if r == cible]
        if len(candidats) == 1:
            liens.append(_lien(candidats[0], noeud["key"]))

    # Sites : sous le routeur qui les dessert. Le site PROPRE d'un routeur (son
    # PoP) est deja sa case : ses clients s'y accrochent par le nom.
    mesures = {(str(m.get("router_name")), str(m.get("interface"))): m for m in interfaces}
    noms_routeurs = {n["name"] for n in par_nom.values()}
    vus: set[str] = set()
    for s in sorted(sites, key=lambda s: str(s.get("name") or "")):
        nom = str(s.get("name") or "")
        routeur = par_nom.get(str(s.get("router_name") or ""))
        if not nom or routeur is None or nom in noms_routeurs or nom in vus:
            continue
        if str(s.get("kind") or "") == "router":
            continue
        vus.add(nom)
        cle = site_node_key(nom)
        interface = s.get("vlan_interface")
        noeuds.append(
            {
                "key": cle,
                "name": nom,
                "kind": "vlan" if s.get("kind") == "vlan" else "sector",
                "router_name": routeur["router_name"],
                "attributes": {
                    "declared": True,
                    "site": True,
                    "vlan_id": s.get("vlan_id"),
                    "vlan_interface": interface,
                },
                "parent_override": None,
                "config_parent": None,
                "hidden": False,
                "fresh": True,
            }
        )
        mesure = mesures.get((routeur["router_name"], str(interface))) if interface else None
        champs: dict[str, Any] = {"source_name": routeur["name"], "target_name": nom}
        if mesure is not None:
            champs.update(
                interface=interface,
                discovered_by=routeur["router_name"],
                rx_bps=mesure.get("rx_bps"),
                tx_bps=mesure.get("tx_bps"),
                running=mesure.get("running"),
                port_capacity_mbps=mesure.get("capacity_mbps"),
                measured_at=mesure.get("ts"),
                measure_fresh=mesure.get("fresh"),
                interface_links=1,
            )
        liens.append(_lien(routeur["key"], cle, **champs))

    noms = {n["key"]: n["name"] for n in noeuds}
    for lien in liens:
        lien.setdefault("source_name", noms.get(lien["source_key"]))
        lien.setdefault("target_name", noms.get(lien["target_key"]))
    return {"nodes": noeuds, "links": liens}


def _attributs(ligne: dict[str, Any]) -> dict[str, Any]:
    brut = ligne.get("attributes")
    if isinstance(brut, dict):
        return dict(brut)
    if isinstance(brut, str) and brut:
        try:
            valeur = json.loads(brut)
        except ValueError:
            return {}
        return valeur if isinstance(valeur, dict) else {}
    return {}
