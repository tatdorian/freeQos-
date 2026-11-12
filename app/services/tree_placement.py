"""Ou pend chaque client dans l'arbre reseau -- automatiquement, et modifiable.

L'arbre ne servait plus que les EQUIPEMENTS : les cases des clients a IP fixe
et des CPE d'abonnes etaient retirees (pour ne pas compter deux fois le meme
client), et l'interface rattachait ensuite chaque client a la case qui portait
EXACTEMENT le nom de son site. Un client sous un secteur radio se retrouvait a
plat sous le PoP ; un client range dans un site de VLAN (« VLAN 2060 »)
disparaissait, aucune case ne portant ce nom.

Ici, chaque client recoit une place, et la facon dont on l'a trouvee :

1. ``manual``    -- l'exploitant l'a pose la (glisser-deposer, « Attach here ») ;
2. ``device``    -- un equipement decouvert porte son nom (identite MNDP egale
   au login, au nom saisi ou au libelle du client) : la case EST le client ;
3. ``graph``     -- sa propre case (IP fixe, CPE reconnu par MAC ou adresse)
   pend sous un equipement : il pend sous le meme ;
4. ``sector``    -- son rattachement a un secteur radio (caller-id, fiche) ;
5. ``vlan-site`` -- son site est une VLAN : il pend sous le routeur qui la porte,
   dans une case « VLAN ... » ;
6. ``site``      -- la case du site qui porte son nom ;
7. ``unplaced``  -- rien de tout cela : il reste visible, dans « Unplaced ».
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

#: Roles d'infrastructure : un client qui porterait le nom d'un PoP ne devient
#: jamais ce PoP.
INFRASTRUCTURE = {"pop", "core", "gateway", "router", "backhaul"}


def normalise(nom: Any) -> str:
    """« Nestlé SA » == « nestle-sa » == « NESTLE_SA »."""
    texte = unicodedata.normalize("NFKD", str(nom or ""))
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", texte.lower())


def _attributs(noeud: Mapping[str, Any]) -> dict[str, Any]:
    brut = noeud.get("attributes")
    if isinstance(brut, str):
        try:
            brut = json.loads(brut)
        except ValueError:
            return {}
    return dict(brut) if isinstance(brut, dict) else {}


def _role(noeud: Mapping[str, Any]) -> str:
    return str(noeud.get("kind_override") or noeud.get("kind") or "")


def client_of_hidden_node(noeud: Mapping[str, Any]) -> str | None:
    """Le client que represente une case retiree de l'arbre (fiche IP fixe, CPE)."""
    attrs = _attributs(noeud)
    if attrs.get("subscriber_cpe"):
        return str(attrs["subscriber_cpe"])
    if _role(noeud) == "static":
        return str(attrs.get("reference") or noeud.get("name") or "") or None
    return None


def place_clients(
    *,
    visible: Iterable[Mapping[str, Any]],
    hidden: Iterable[Mapping[str, Any]],
    links: Iterable[Mapping[str, Any]],
    clients: Iterable[Mapping[str, Any]],
    attachments: Mapping[str, str],
    pop_sites: Iterable[Mapping[str, Any]],
    manual_places: Mapping[str, str],
    manual_links: Mapping[str, str],
    names: Mapping[str, Iterable[str]],
) -> dict[str, Any]:
    """``placements`` (login -> {node, how, site?}) et ``client_nodes``
    (cle de case -> {login, how}) : de quoi dessiner chaque client a sa place.

    ``clients`` : lignes {login, pop_name, kind}. ``names`` : pour chaque login,
    ses autres noms (nom saisi, libelle). ``manual_links`` : case -> login,
    pose a la main (« this box is client X »), ou ``""`` pour refuser un lien
    automatique.
    """
    noeuds = {str(n["key"]): n for n in visible}
    clients = list(clients)
    logins = {str(c["login"]) for c in clients}

    # --- 1. Cases qui SONT un client (lien manuel, sinon nom identique) -----
    client_nodes: dict[str, dict[str, str]] = {}
    pris: set[str] = set()
    for cle, login in manual_links.items():
        if cle in noeuds and login and login in logins and login not in pris:
            client_nodes[cle] = {"login": login, "how": "manual"}
            pris.add(login)
    par_nom: dict[str, set[str]] = {}
    for login in logins:
        for nom in (login, *names.get(login, ())):
            if normalise(nom):
                par_nom.setdefault(normalise(nom), set()).add(login)
    candidats: dict[str, set[str]] = {}
    for cle, noeud in noeuds.items():
        if cle in manual_links or _role(noeud) in INFRASTRUCTURE:
            continue
        if _attributs(noeud).get("managed") is True:
            continue
        trouves = par_nom.get(normalise(noeud.get("name")), set()) - pris
        if len(trouves) == 1:
            candidats[cle] = trouves
    # Un nom porte par DEUX cases ne designe personne avec certitude.
    compte: dict[str, int] = {}
    for trouves in candidats.values():
        for login in trouves:
            compte[login] = compte.get(login, 0) + 1
    for cle, trouves in candidats.items():
        (login,) = tuple(trouves)
        if compte[login] == 1:
            client_nodes[cle] = {"login": login, "how": "name"}
            pris.add(login)
    case_du_client = {v["login"]: cle for cle, v in client_nodes.items()}

    # --- index pour les rattachements automatiques ---------------------------
    voisins: dict[str, list[str]] = {}
    for lien in links:
        a, b = str(lien.get("source_key")), str(lien.get("target_key"))
        voisins.setdefault(a, []).append(b)
        voisins.setdefault(b, []).append(a)
    parent_du_cache: dict[str, str] = {}
    for noeud in hidden:
        du_cache = client_of_hidden_node(noeud)
        if not du_cache:
            continue
        parent = next((v for v in voisins.get(str(noeud["key"]), []) if v in noeuds), None)
        if parent:
            parent_du_cache.setdefault(du_cache, parent)
    par_nom_de_site: dict[str, str] = {}
    for cle, noeud in noeuds.items():
        nom = normalise(noeud.get("name"))
        if nom and (nom not in par_nom_de_site or _role(noeud) == "pop"):
            par_nom_de_site[nom] = cle
    routeur_du_site = {
        normalise(s.get("name")): str(s.get("router_name"))
        for s in pop_sites
        if s.get("router_name")
    }
    case_du_routeur: dict[str, str] = {}
    for cle, noeud in noeuds.items():
        if noeud.get("router_name"):
            case_du_routeur.setdefault(str(noeud["router_name"]), cle)
        if cle.startswith("router:"):
            case_du_routeur.setdefault(cle.split(":", 1)[1], cle)

    # --- 2. Une place pour chaque client ------------------------------------
    placements: dict[str, dict[str, Any]] = {}
    for client in clients:
        login = str(client["login"])
        site = str(client.get("pop_name") or "")
        manuel = manual_places.get(login)
        if manuel and manuel in noeuds:
            placements[login] = {"node": manuel, "how": "manual"}
        elif login in case_du_client:
            placements[login] = {"node": case_du_client[login], "how": "device"}
        elif login in parent_du_cache:
            placements[login] = {"node": parent_du_cache[login], "how": "graph"}
        elif attachments.get(login) in noeuds:
            placements[login] = {"node": attachments[login], "how": "sector"}
        elif normalise(site) in par_nom_de_site:
            placements[login] = {"node": par_nom_de_site[normalise(site)], "how": "site"}
        elif case_du_routeur.get(routeur_du_site.get(normalise(site), "")):
            placements[login] = {
                "node": case_du_routeur[routeur_du_site[normalise(site)]],
                "how": "vlan-site",
                "site": site,
            }
        else:
            placements[login] = {"node": None, "how": "unplaced", "site": site or None}
    return {"placements": placements, "client_nodes": client_nodes}
