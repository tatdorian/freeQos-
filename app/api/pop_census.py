"""Recensement d'un PoP : qui vit dessus, et lesquels sont declares.

CE QUE CET ENDPOINT REPOND
--------------------------
"Montre-moi TOUS les clients de ce PoP." La question parait simple ; elle ne
l'est pas, parce que les clients d'un PoP n'ont pas tous la meme trace : une
session PPPoE se nomme, un client a IP fixe ne laisse qu'une entree ARP, un
client DHCP un bail, un client derriere un pont en filtrage VLAN une entree ARP
au nom du PONT, et un client a qui on a route un /29 ne laisse rien du tout --
sauf une route.

``pop_census`` croise ces sources, routeur par routeur. Cet endpoint fait les
deux choses qui restent :

  - il regroupe par PoP, parce que c'est l'unite d'exploitation ;
  - il RAPPROCHE de l'inventaire declare. C'est la colonne qui compte : sans
    elle, l'exploitant a une liste d'adresses ; avec elle, il a la liste de ce
    qu'il ne connaissait pas.

LECTURE SEULE, ET RIEN N'EST CREE. Le recensement propose ; declarer reste un
geste humain, dans ``POST /static-clients``, avec un debit souscrit que seul
l'exploitant connait.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import ContainerDep
from app.collectors.pop_census import NATURE_CLIENT, NATURE_PPPOE, PopCensus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["static clients"])


def _reseau_declare(fiche: dict[str, Any]) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """Le bloc d'un client declare, tel que la base le stocke.

    L'inventaire garde l'adresse et la longueur de prefixe separement : un
    client en /29 doit etre reconnu quand n'importe laquelle de ses adresses
    parle, exactement comme le fait le ``<<=`` cote SQL.
    """
    adresse = fiche.get("address")
    longueur = fiche.get("prefix_len")
    if not adresse:
        return None
    try:
        return ipaddress.ip_network(f"{adresse}/{longueur}" if longueur else str(adresse))
    except ValueError:
        return None


def _rapprocher(hote: dict[str, Any], fiches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """La fiche declaree qui contient cette adresse, la plus precise d'abord."""
    try:
        adresse = ipaddress.ip_address(str(hote["address"]))
    except ValueError:
        return None
    trouvees: list[tuple[int, dict[str, Any]]] = []
    for fiche in fiches:
        reseau = _reseau_declare(fiche)
        if reseau is None or reseau.version != adresse.version:
            continue
        if adresse in reseau:
            trouvees.append((reseau.prefixlen, fiche))
    if not trouvees:
        return None
    trouvees.sort(key=lambda paire: -paire[0])
    fiche = trouvees[0][1]
    return {
        "reference": fiche.get("reference"),
        "label": fiche.get("label"),
        "address": fiche.get("address"),
        "prefix_len": fiche.get("prefix_len"),
        "plan_down_mbps": fiche.get("plan_down_mbps"),
        "plan_up_mbps": fiche.get("plan_up_mbps"),
        "enabled": fiche.get("enabled"),
    }


async def _equipements_connus(container: ContainerDep) -> list[str]:
    """Adresses de management de nos propres equipements.

    Sans elles, le routeur voisin et l'antenne du secteur reapparaissent en
    "client possible" a chaque recensement. Une liste dont la moitie est du
    materiel reseau finit par ne plus etre lue -- et c'est la liste qui compte.
    """
    adresses = [c.config.host for c in container.registry.collectors if c.config.host]
    depot = container.antennas_repo
    if depot is not None:
        try:
            adresses += [str(row["host"]) for row in await depot.list_public() if row.get("host")]
        except Exception:  # noqa: BLE001 - une antenne illisible ne casse pas le recensement
            logger.exception("Inventaire des antennes illisible pour le recensement")
    return adresses


@router.get("/pops/census", summary="Census of the clients of a PoP, every kind")
async def census(
    container: ContainerDep,
    pop_name: Annotated[str | None, Query(max_length=128)] = None,
    router_name: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    """Croise sept sources par routeur, regroupe par PoP, rapproche des fiches.

    Une quinzaine de ``print`` en lecture seule par routeur : c'est une lecture
    a la demande, pas une metrique. Un routeur muet ne fait pas echouer les
    autres -- son erreur est rendue a sa place, parce qu'un PoP absent d'un
    recensement silencieux serait exactement le piege a eviter.
    """
    collecteurs = [
        c
        for c in container.registry.collectors
        if router_name in (None, c.name) and pop_name in (None, c.config.effective_pop_name)
    ]
    if not collecteurs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No collected router matches. A router dropped from collection "
                "is read nowhere: check the Devices tab."
            ),
        )

    fiches: list[dict[str, Any]] = []
    if container.static_clients_repo is not None:
        try:
            fiches = await container.static_clients_repo.list_all(pop_name=pop_name)
        except Exception:  # noqa: BLE001 - le recensement vaut mieux que rien
            logger.exception("Inventaire des clients statiques illisible")

    materiel = await _equipements_connus(container)

    pops: dict[str, dict[str, Any]] = {}
    for collecteur in collecteurs:
        nom_pop = collecteur.config.effective_pop_name
        pop = pops.setdefault(
            nom_pop,
            {"pop_name": nom_pop, "routers": [], "clients": [], "remarks": [], "errors": []},
        )
        try:
            recensement: PopCensus = await collecteur.census(known_equipment=materiel)
        except Exception as exc:  # noqa: BLE001 - un routeur muet ne casse pas le PoP
            pop["errors"].append(f"{collecteur.name}: {type(exc).__name__}: {exc}")
            continue

        rapport = recensement.as_dict()
        pop["routers"].append(rapport)
        pop["remarks"] += [f"{collecteur.name} : {note}" for note in recensement.remarks]
        for hote in rapport["hosts"]:
            if hote["nature"] not in (NATURE_CLIENT, NATURE_PPPOE):
                continue
            pop["clients"].append(
                {**hote, "router": collecteur.name, "declared": _rapprocher(hote, fiches)}
            )

    for pop in pops.values():
        pop["clients"].sort(key=lambda c: (c["nature"] != NATURE_CLIENT, str(c["address"])))
        pop["counts"] = {
            "clients": len(pop["clients"]),
            "pppoe": sum(1 for c in pop["clients"] if c["nature"] == NATURE_PPPOE),
            "declares": sum(1 for c in pop["clients"] if c["declared"] or c["login"]),
            # LE CHIFFRE QUI COMPTE : ce que le PoP porte et que l'inventaire
            # ignore. C'est la seule facon de savoir qu'il manque quelqu'un.
            "non_declares": sum(1 for c in pop["clients"] if not c["declared"] and not c["login"]),
        }

    return {"pops": [pops[nom] for nom in sorted(pops)]}
