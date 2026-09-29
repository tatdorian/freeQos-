"""Menage des PoPs vides : un site affiche doit porter quelque chose.

Un PoP entre en base des qu'un nom de site est vu (routeur, VLAN, fiche). Il y
reste quand plus rien ne le porte : routeur retire de l'inventaire, essai
("PoP Nord", "PoP Sud") jamais relie a un equipement. Il encombre alors
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
    return noms


async def purge_empty_pops(container: Any) -> list[str]:
    retires: list[str] = await container.repository.purge_empty_pops(
        keep=await declared_pop_names(container)
    )
    if retires:
        # Les identifiants en cache designeraient des lignes disparues.
        container.directory.clear_cache()
        logger.info("PoPs vides retires : %s", ", ".join(retires))
    return retires
