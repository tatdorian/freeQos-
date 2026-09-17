"""Capacite : ce qui est vendu, ce qui porte, et l'usage entre les deux.

CE QUE CETTE PAGE REPOND, ET QUE RIEN D'AUTRE NE REPONDAIT
----------------------------------------------------------
Le reste de l'interface regarde l'INSTANT : qui est en ligne, quel debit passe
maintenant, quelle latence. Quatre questions d'exploitation portent sur la
DUREE, et la donnee etait deja en base sans que personne ne la croise :

  - combien ai-je vendu sur ce PoP, et qu'est-ce qui le porte ?
  - quand mes liens saturent-ils, et a quelle part de leur capacite ?
  - qui consomme en VOLUME -- pas qui telecharge a cet instant ?
  - qui ne consomme plus rien du tout, alors qu'il est toujours declare ?

Ces quatre reponses servent a dimensionner, pas a depanner. C'est pour cela
qu'elles vivent sur une page a part, et qu'elles se lisent sur des jours plutot
que sur des minutes.

TOUT EST EN LECTURE. Aucune de ces analyses n'ecrit quoi que ce soit, ni en
base, ni sur un routeur.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import RepositoryDep
from app.services.capacity import link_row, pop_capacity_row, usage_row

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capacite"])


@router.get("/capacity", summary="Capacite vendue, occupation des liens, usage reel")
async def capacity(
    repo: RepositoryDep,
    hours: Annotated[int, Query(ge=1, le=720, description="Fenetre des pointes")] = 24,
    usage_hours: Annotated[
        int, Query(ge=1, le=2160, description="Fenetre des volumes consommes")
    ] = 168,
    silent_days: Annotated[
        int, Query(ge=1, le=365, description="Silence au-dela duquel une ligne est signalee")
    ] = 7,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """Les quatre analyses, en une lecture.

    Les fenetres sont distinctes a dessein : une pointe se cherche sur 24 h (un
    maximum noye dans une semaine ne dit plus a quelle heure il tombe), un
    volume sur une semaine (un jour depend trop du week-end), un silence sur
    plusieurs jours (une coupure d'une nuit n'est pas un depart).
    """
    pops = await repo.capacity_by_pop(hours=hours)
    liens = await repo.link_occupancy(hours=hours, limit=limit)
    usage = await repo.subscriber_usage(hours=usage_hours, limit=limit)
    muets = await repo.silent_subscribers(days=silent_days, limit=limit)

    lignes_pops = [pop_capacity_row(row) for row in pops]
    lignes_liens = [link_row(row) for row in liens]
    lignes_usage = [usage_row(row) for row in usage]

    vendu = sum(ligne["sold_down_mbps"] for ligne in lignes_pops)
    capacite = sum(ligne["capacity_mbps"] or 0.0 for ligne in lignes_pops)
    return {
        "window": {"hours": hours, "usage_hours": usage_hours, "silent_days": silent_days},
        "totals": {
            "sold_down_mbps": round(vendu, 1),
            "capacity_mbps": round(capacite, 1),
            # Le rapport du reseau entier. Il ne remplace pas celui de chaque
            # PoP : un site tres survendu se noie dans une moyenne, et c'est
            # pourtant lui qui appellera au telephone.
            "ratio": round(vendu / capacite, 2) if capacite else None,
            "subscribers_at_ceiling": sum(1 for u in lignes_usage if u["at_plan_ceiling"]),
            "silent": len(muets),
        },
        "pops": lignes_pops,
        "links": lignes_liens,
        "usage": lignes_usage,
        "silent": muets,
    }
