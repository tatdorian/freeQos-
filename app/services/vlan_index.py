"""Un client declare sur une VLAN possede le RESEAU de cette VLAN.

Constate chez l'exploitant : 100.100.105.242, adresse du routeur d'un client
pose sur une VLAN, s'affichait "undeclared" dans le trafic. Tout etait pourtant
connu : la table d'adresses du NAS donne le reseau de l'interface VLAN
(100.100.105.240/30 sur vlan2060), l'analyse de configuration donne le numero
de VLAN de cette interface, et la fiche du client porte ce numero. Il manquait
seulement le rapprochement : l'index ne connaissait que l'adresse SAISIE.

Regle : sur un routeur, si UN SEUL client est declare sur la VLAN V, tout le
reseau de l'interface de la VLAN V lui appartient. Deux clients sur la meme
VLAN : on ne devine pas -- leurs adresses declarees continuent de faire foi.

Fonction pure : la decouverte fournit les reseaux et les VLAN, l'inventaire
les clients ; rien n'est lu ni ecrit ici.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def vlan_prefixes(
    clients: Iterable[tuple[int, int, set[str]]],
    vlans_par_routeur: Mapping[str, Mapping[str, int | None]],
    reseaux_par_routeur: Mapping[str, Sequence[tuple[Any, str | None]]],
) -> list[tuple[str, int]]:
    """``[(reseau, abonne)]`` pour les VLAN qui n'ont qu'un client declare.

    ``clients`` : (id d'abonne, numero de VLAN, routeurs qui desservent sa fiche).
    ``vlans_par_routeur`` : routeur -> interface -> numero de VLAN.
    ``reseaux_par_routeur`` : routeur -> [(reseau connecte, interface)].
    """
    par_vlan: dict[tuple[str, int], set[int]] = {}
    for abonne, vlan, routeurs in clients:
        for routeur in routeurs:
            par_vlan.setdefault((routeur, vlan), set()).add(abonne)
    sortie: list[tuple[str, int]] = []
    for routeur, reseaux in reseaux_par_routeur.items():
        vlans = vlans_par_routeur.get(routeur) or {}
        for reseau, interface in reseaux:
            numero = vlans.get(interface or "")
            if numero is None:
                continue
            seuls = par_vlan.get((routeur, numero)) or set()
            if len(seuls) == 1:
                bloc = str(ipaddress.ip_network(str(reseau), strict=False))
                sortie.append((bloc, next(iter(seuls))))
    return sortie


def where_is(address: str | None) -> dict[str, Any] | None:
    """Ou se trouve une adresse non rattachee : routeur et interface qui la portent."""
    from app.collectors.mikrotik import router_serving

    porteur = router_serving(address)
    if porteur is None:
        return None
    return {"router": porteur[0], "interface": porteur[1]}
