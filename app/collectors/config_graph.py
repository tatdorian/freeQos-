"""Analyse de la CONFIGURATION des routeurs pour reconstruire l'arbre reel.

POURQUOI UN MODULE A PART
-------------------------
La decouverte par voisinage (``/ip/neighbor``) repond a une question faible :
"qui se voit ?". Elle ne sait ni qui est au-dessus de qui, ni ce qui passe par
ou. L'arbre devait donc etre DEDUIT -- une racine choisie par heuristique, des
parents calcules par plus court chemin -- et le resultat ressemblait au reseau
sans etre le reseau.

La configuration, elle, repond aux questions fortes, parce que c'est elle qui
FAIT le reseau :

  - ``/ip/route``            : ou part ce que je ne sais pas router. C'est la
                               relation hierarchique elle-meme.
  - OSPF / BGP               : avec qui j'echange REELLEMENT des routes. Une
                               adjacence etablie, pas un equipement apercu sur
                               un switch.
  - VLAN, bridge, bonding    : par quel port physique sort un trafic donne.
                               C'est ce qui rattache un client a son vrai lien.

Ce module ne fait aucune entree-sortie : il recoit des tables RouterOS deja
lues et rend des structures. C'est ce qui le rend testable sans routeur, et
c'est aussi ce qui permet de l'appliquer a un ``/export`` colle a la main.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.collectors.parsing import parse_flag

logger = logging.getLogger(__name__)

# Au-dela, on considere qu'on tourne en rond dans un empilement d'interfaces
# (bridge dans bridge, VLAN sur VLAN mal declaree).
_PROFONDEUR_MAX = 8

DEFAULT_ROUTES = ("0.0.0.0/0", "::/0")


def _texte(valeur: Any) -> str:
    return str(valeur or "").strip()


def _adresse_nue(valeur: Any) -> str | None:
    """Extrait l'adresse d'un champ RouterOS.

    RouterOS ecrit selon les cas ``10.0.0.1``, ``10.0.0.1/30``, ou encore
    ``10.0.0.1%ether1`` pour designer la sortie. On ne garde que l'adresse.
    """
    texte = _texte(valeur).split("%")[0].split("/")[0].strip()
    if not texte:
        return None
    try:
        adresse = ipaddress.ip_address(texte)
    except ValueError:
        return None
    if adresse.is_unspecified or adresse.is_loopback:
        return None
    return str(adresse)


# =========================================================================
# Empilement des interfaces
# =========================================================================


@dataclass(slots=True)
class InterfacePath:
    """Ce qui se cache sous une interface logique.

    ``ports`` liste les interfaces PHYSIQUES qui portent reellement le trafic.
    Un VLAN sur un bridge de quatre ports en a quatre : le trafic peut sortir
    par n'importe lequel, et c'est une information honnete -- pretendre en
    choisir un seul serait inventer.
    """

    name: str
    kind: str  # "physical" | "vlan" | "bridge" | "bonding"
    ports: list[str] = field(default_factory=list)
    vlan_id: int | None = None
    parent: str | None = None
    # Vrai quand l'empilement n'a pas pu etre resolu (boucle, parent absent).
    broken: bool = False


def interface_stacks(
    *,
    interfaces: Sequence[dict[str, Any]] = (),
    vlans: Sequence[dict[str, Any]] = (),
    bridge_ports: Sequence[dict[str, Any]] = (),
    bondings: Sequence[dict[str, Any]] = (),
) -> dict[str, InterfacePath]:
    """Resout chaque interface logique jusqu'a ses ports physiques.

    Sans cela, savoir qu'un client est sur ``vlan120`` ne dit rien : il faut
    savoir que ``vlan120`` est posee sur ``bridge-acces``, lui-meme compose de
    ``ether3`` et ``ether4``, pour rattacher ce client au bon lien -- et donc
    pour que le partage d'un lien congestionne le compte au bon endroit.
    """
    parents: dict[str, str] = {}
    membres: dict[str, list[str]] = {}
    natures: dict[str, str] = {}
    vlan_ids: dict[str, int | None] = {}

    for row in vlans:
        nom = _texte(row.get("name"))
        parent = _texte(row.get("interface"))
        if not nom:
            continue
        natures[nom] = "vlan"
        if parent:
            parents[nom] = parent
        brut = _texte(row.get("vlan-id")) or _texte(row.get("vlan_id"))
        try:
            vlan_ids[nom] = int(brut) if brut else None
        except ValueError:
            vlan_ids[nom] = None

    for row in bridge_ports:
        if parse_flag(row.get("disabled")):
            continue
        pont = _texte(row.get("bridge"))
        port = _texte(row.get("interface"))
        if pont and port:
            natures.setdefault(pont, "bridge")
            membres.setdefault(pont, []).append(port)

    for row in bondings:
        nom = _texte(row.get("name"))
        if not nom:
            continue
        natures[nom] = "bonding"
        esclaves = [s.strip() for s in _texte(row.get("slaves")).split(",") if s.strip()]
        if esclaves:
            membres.setdefault(nom, []).extend(esclaves)

    connues = {_texte(row.get("name")) for row in interfaces if _texte(row.get("name"))}
    connues |= set(natures) | set(parents) | {p for liste in membres.values() for p in liste}

    def resoudre(nom: str, vus: set[str], profondeur: int) -> tuple[list[str], bool]:
        """Descend jusqu'aux ports physiques. Renvoie ``(ports, casse)``."""
        if nom in vus or profondeur > _PROFONDEUR_MAX:
            # Empilement circulaire : on s'arrete et on le DIT, plutot que de
            # rendre une liste plausible mais fausse.
            return [], True
        vus = vus | {nom}
        if nom in membres:
            ports: list[str] = []
            casse = False
            for membre in membres[nom]:
                sous_ports, sous_casse = resoudre(membre, vus, profondeur + 1)
                casse = casse or sous_casse
                ports.extend(sous_ports or [membre])
            return _sans_doublon(ports), casse
        if nom in parents:
            return resoudre(parents[nom], vus, profondeur + 1)
        return [nom], False

    resultat: dict[str, InterfacePath] = {}
    for nom in sorted(connues):
        ports, casse = resoudre(nom, set(), 0)
        resultat[nom] = InterfacePath(
            name=nom,
            kind=natures.get(nom, "physical"),
            ports=ports,
            vlan_id=vlan_ids.get(nom),
            parent=parents.get(nom),
            broken=casse,
        )
    return resultat


def _sans_doublon(valeurs: Sequence[str]) -> list[str]:
    vus: list[str] = []
    for valeur in valeurs:
        if valeur and valeur not in vus:
            vus.append(valeur)
    return vus


# =========================================================================
# Hierarchie : qui est au-dessus de qui
# =========================================================================


@dataclass(slots=True)
class Upstream:
    """Ou part le trafic que ce routeur ne sait pas router."""

    gateway: str
    distance: int
    interface: str | None = None


def default_gateways(routes: Sequence[dict[str, Any]]) -> list[Upstream]:
    """Passerelles des routes par defaut ACTIVES, de la meilleure a la pire.

    Seules les routes actives comptent : une route de secours decrit ce qui se
    passerait en cas de panne, pas la topologie du moment. Les routes
    desactivees, elles, ne decrivent rien du tout.

    Plusieurs passerelles a egalite de distance = routeur multi-homé. On les
    rend toutes : c'est a l'appelant de refuser d'en choisir une, parce qu'un
    arbre n'a qu'un parent et qu'inventer lequel serait mentir.
    """
    trouvees: list[Upstream] = []
    for row in routes:
        if parse_flag(row.get("disabled")):
            continue
        # 'active' absent = on suppose active (certaines versions ne le posent
        # que sur les routes dynamiques).
        actif = parse_flag(row.get("active"))
        if actif is False:
            continue
        dst = _texte(row.get("dst-address")) or _texte(row.get("dst_address"))
        if dst not in DEFAULT_ROUTES:
            continue
        for champ in ("gateway", "immediate-gw", "immediate_gw"):
            adresse = _adresse_nue(row.get(champ))
            if adresse:
                brut_distance = _texte(row.get("distance"))
                try:
                    distance = int(brut_distance) if brut_distance else 1
                except ValueError:
                    distance = 1
                interface = (
                    _texte(row.get(champ)).split("%")[1] if "%" in _texte(row.get(champ)) else None
                )
                trouvees.append(Upstream(adresse, distance, interface))
                break
    trouvees.sort(key=lambda u: (u.distance, u.gateway))
    return trouvees


def best_upstream(routes: Sequence[dict[str, Any]]) -> tuple[Upstream | None, str]:
    """La passerelle qui fait foi, ou None avec la raison.

    ``("ambigu")`` quand plusieurs passerelles se valent : un routeur
    multi-homé n'a pas UN parent, et le dire est plus utile que d'en designer
    un au hasard qui changera au prochain cycle.
    """
    passerelles = default_gateways(routes)
    if not passerelles:
        return None, "aucune route par defaut"
    meilleure = passerelles[0]
    exaequo = [u for u in passerelles if u.distance == meilleure.distance]
    if len({u.gateway for u in exaequo}) > 1:
        return None, "ambigu"
    return meilleure, "route par defaut"


# =========================================================================
# Adjacences de routage : des liens PROUVES
# =========================================================================


def routing_peers(
    *,
    ospf_neighbors: Sequence[dict[str, Any]] = (),
    bgp_sessions: Sequence[dict[str, Any]] = (),
) -> list[str]:
    """Adresses des pairs de routage ETABLIS.

    Une adjacence OSPF en etat ``Full`` ou une session BGP etablie prouvent que
    les deux routeurs se parlent vraiment. C'est plus fort qu'un voisinage
    MNDP, qui ne prouve qu'une proximite L2 -- deux equipements branches sur le
    meme switch se voient sans rien echanger.
    """
    pairs: list[str] = []

    for row in ospf_neighbors:
        etat = _texte(row.get("state")).lower()
        # 'Full' est l'etat nominal ; '2-Way' existe sur un segment diffuse
        # entre routeurs non designes, et reste une adjacence reelle.
        if etat and not (etat.startswith("full") or etat.startswith("2-way")):
            continue
        for champ in ("address", "router-id", "router_id"):
            adresse = _adresse_nue(row.get(champ))
            if adresse and adresse not in pairs:
                pairs.append(adresse)

    for row in bgp_sessions:
        etabli = parse_flag(row.get("established"))
        etat = _texte(row.get("state")).lower()
        if etabli is False or (etat and etat != "established"):
            continue
        for champ in ("remote.address", "remote-address", "remote.id", "remote-id"):
            adresse = _adresse_nue(row.get(champ))
            if adresse and adresse not in pairs:
                pairs.append(adresse)

    return pairs
