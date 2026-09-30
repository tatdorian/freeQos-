"""Menage des PoPs vides : un site affiche doit porter quelque chose.

Un PoP entre en base des qu'un nom de site est vu (routeur, VLAN, fiche). Il y
reste quand plus rien ne le porte : routeur retire de l'inventaire, essai
("PoP Altair", "PoP Vega") jamais relie a un equipement. Il encombre alors
l'arbre, les listes et les filtres sans rien mesurer.

Ce job retire ceux qui n'ont ni abonne ni backhaul ET dont le nom n'est
declare nulle part. Un site declare mais encore vide (routeur ajoute, pas
encore de session) est garde.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

JOB_PURGE_POPS = "purge_empty_pops"


async def declared_pop_names(container: Any) -> set[str]:
    """Tous les noms de site qu'un routeur, une antenne ou un client porte."""
    noms: set[str] = set()

    def ajoute(*valeurs: Any) -> None:
        for v in valeurs:
            if v:
                noms.add(str(v))

    for routeur in container.settings.routers:
        ajoute(routeur.name, routeur.effective_pop_name)
    for lien in container.settings.backhauls:
        ajoute(lien.pop_name)
    for collecteur in getattr(container.collection, "collectors", []) or []:
        ajoute(collecteur.name, collecteur.config.effective_pop_name)
    if container.routers_repo is not None:
        for routeur in await container.routers_repo.load_configs(enabled_only=False):
            ajoute(routeur.name, routeur.effective_pop_name)
    if container.antennas_repo is not None:
        for antenne in await container.antennas_repo.list_public():
            ajoute(antenne.get("pop_name"))
    if container.static_clients_repo is not None:
        for client in await container.static_clients_repo.list_all():
            ajoute(client.get("pop_name"))
    # Sites pousses par l'API (contrat Preseem) : ils ont leur place dans
    # l'application, meme avant qu'un routeur ou un client les porte.
    modele = getattr(container, "model_repo", None)
    if modele is not None:
        for site in await modele.list_objects("sites"):
            ajoute(site.get("name"))
    return noms


#: Anciens PoPs de demonstration. DEMANDE EXPLICITE : aucune trace ne doit en
#: rester, ni a l'ecran ni en base.
NOMS_RETIRES = ("pop-nord", "pop-sud", "PoP Nord", "PoP Sud")


async def purge_removed_routers(container: Any) -> list[str]:
    """Efface pour de bon les routeurs retires et les anciens PoPs de demo.

    Un routeur "retire de l'inventaire fichier" laissait un marqueur en base et
    un bandeau "Restore" dans l'interface, plus son site, ses abonnes et leur
    historique. L'inventaire fichier n'est plus monte par le deploiement : ces
    marqueurs ne designent plus rien. Tout ce qui s'y rattache est supprime,
    sauf un site qu'un routeur ACTIF porte encore.
    """
    from app.services.pop_match import normalise_pop

    noms: set[str] = set(NOMS_RETIRES)
    routeurs_repo = getattr(container, "routers_repo", None)
    if routeurs_repo is not None:
        masques = await routeurs_repo.hidden_file_routers()
        noms |= masques
        effacer = getattr(routeurs_repo, "delete_by_names", None)
        if effacer is not None:
            await effacer(sorted(noms))
        # Un marqueur ne sert que tant que le fichier declare encore ce routeur.
        oublier = getattr(routeurs_repo, "clear_hidden_file_routers", None)
        if oublier is not None:
            fichier = {r.name for r in container.settings.routers}
            await oublier(keep=masques & fichier)
    cibles = {normalise_pop(n) for n in noms} | {n.lower() for n in noms}

    actifs: set[str] = set()
    for routeur in container.settings.routers:
        if routeur.name.lower() not in cibles:
            actifs |= {routeur.name.lower(), normalise_pop(routeur.effective_pop_name)}
    if routeurs_repo is not None:
        for routeur in await routeurs_repo.load_configs(enabled_only=False):
            actifs |= {routeur.name.lower(), normalise_pop(routeur.effective_pop_name)}

    retires: list[str] = []
    depot = getattr(container, "repository", None)
    if depot is None or not (hasattr(depot, "list_pops") and hasattr(depot, "delete_pop")):
        return retires
    for pop in await depot.list_pops():
        nom = str(pop.get("name") or "")
        porteur = str(pop.get("router_name") or "").lower()
        vise = normalise_pop(nom) in cibles or nom.lower() in cibles or porteur in cibles
        if vise and normalise_pop(nom) not in actifs and porteur not in actifs:
            await depot.delete_pop(int(pop["id"]))
            retires.append(nom)
    if retires:
        container.directory.clear_cache()
        logger.info("Sites retires definitivement : %s", ", ".join(retires))
    return retires


async def purge_empty_pops(container: Any) -> list[str]:
    retires: list[str] = await container.repository.purge_empty_pops(
        keep=await declared_pop_names(container)
    )
    if retires:
        # Les identifiants en cache designeraient des lignes disparues.
        container.directory.clear_cache()
        logger.info("PoPs vides retires : %s", ", ".join(retires))
    return retires
