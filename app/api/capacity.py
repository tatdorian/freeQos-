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

Ces analyses sont en lecture : elles n'ecrivent rien sur un routeur. Seule la
NATURE d'un lien (filaire ou radio, ``/capacity/media``) s'enregistre en base :
c'est elle qui dit d'ou vient la capacite de ce lien.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import CollectionDep, ContainerDep, RepositoryDep
from app.services.capacity import (
    RADIO_CHUTE,
    a_renforcer,
    hotspot_rows,
    link_row,
    node_uplinks,
    pop_capacity_row,
    radio_alerts,
    radio_state,
    usage_row,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capacity"])


@router.get("/capacity", summary="Sold capacity, link occupancy, real usage")
async def capacity(
    repo: RepositoryDep,
    hours: Annotated[int, Query(ge=1, le=720, description="Peak window")] = 24,
    usage_hours: Annotated[
        int, Query(ge=1, le=2160, description="Window of consumed volumes")
    ] = 168,
    silent_days: Annotated[
        int, Query(ge=1, le=365, description="Silence beyond which a row is flagged")
    ] = 7,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """Les quatre analyses, en une lecture.

    Les fenetres sont distinctes a dessein : une pointe se cherche sur 24 h (un
    maximum noye dans une semaine ne dit plus a quelle heure il tombe), un
    volume sur une semaine (un jour depend trop du week-end), un silence sur
    plusieurs jours (une coupure d'une nuit n'est pas un depart).

    ``reinforce`` classe ce qui n'a plus de marge EN MOYENNE, liens et abonnes
    separement : un lien sature se renforce, un abonne sature se vend. Les
    confondre ferait passer une opportunite commerciale pour un probleme
    d'ingenierie.
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
    renfort = a_renforcer(lignes_liens, lignes_usage)
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
            "links_to_reinforce": len(renfort["links"]),
            "subscribers_to_upsell": len(renfort["subscribers"]),
        },
        "pops": lignes_pops,
        "links": lignes_liens,
        "usage": lignes_usage,
        "silent": muets,
        # Ce qui n'a plus de marge EN MOYENNE : un lien a renforcer n'est pas un
        # lien qui a touche son plafond une fois, c'est un lien qui y vit.
        "reinforce": renfort,
    }


@router.get("/capacity/hotspots", summary="Saturation points: headroom left on every link")
async def hotspots(
    repo: RepositoryDep,
    container: ContainerDep,
    collection: CollectionDep,
    hours: Annotated[int, Query(ge=1, le=720, description="Peak window")] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
) -> dict[str, Any]:
    """OU LA MARGE MANQUE, avant que le reseau ne tombe.

    Chaque port mesure, range du plus a risque au moins a risque, avec son cote :
    ``internet`` (la sortie de la passerelle, le transit), ``upstream`` (le lien
    d'un PoP vers le coeur) ou ``pop`` (vers les abonnes, un VLAN, un relais).
    Le risque retient la pointe de la fenetre ET l'instant : une pointe passee
    dit que ca peut recommencer, l'instant dit que c'est en cours.
    """
    from app.collectors.mikrotik import upstream_of

    occupation = await repo.link_occupancy(hours=hours, limit=500)
    liens = await container.topology_repo.links() if container.topology_repo is not None else []
    amonts = {c.name: upstream_of(c.name) for c in collection.collectors}
    roles = {c.name: str(c.config.role) for c in collection.collectors}
    # Le debit ACTUEL de chaque port, et le nom de son voisin : un port que la
    # decouverte n'a pas relie a un lien avait "-" pour maintenant et son nom
    # d'interface pour titre, alors qu'il porte du trafic.
    from app.api.metrics import name_ports

    en_direct = await repo.ports_live()
    name_ports(en_direct, [c.name for c in collection.collectors])
    ports = {(str(p["router_name"]), str(p["interface"])): p for p in en_direct}
    # Nature de chaque lien (filaire / radio) et ce que disent les antennes.
    milieux = await _media(container)
    antennes, maxima = await _antennes(repo)
    toutes = hotspot_rows(
        occupation,
        liens,
        upstream=amonts,
        roles=roles,
        live=ports,
        media={(m["router_name"], m["interface"]): m for m in milieux},
        radios={
            str(b["name"]): {**b, "nominal_fallback_mbps": maxima.get(str(b["name"]))}
            for b in antennes
        },
    )
    montants = node_uplinks(toutes)
    return {
        "hours": hours,
        "thresholds": {"busy": 0.70, "saturated": 0.90, "radio_drop": RADIO_CHUTE},
        "hotspots": toutes[:limit],
        "upstream_known": sorted(n for n, (_g, i) in amonts.items() if i),
        # Le lien montant de chaque routeur, par nom de PoP ET par routeur :
        # c'est la capacite du NOEUD dans "Queues by node".
        "uplinks": {
            **montants,
            **{
                c.config.effective_pop_name: montants[c.name]
                for c in collection.collectors
                if c.name in montants
            },
        },
        "radio_alerts": radio_alerts(antennes, maxima=maxima),
    }


async def _media(container: ContainerDep) -> list[dict[str, Any]]:
    if container.link_media_repo is None:
        return []
    try:
        return await container.link_media_repo.all()
    except Exception:  # noqa: BLE001 - sans declaration, la regle automatique s'applique
        logger.exception("Nature des liens illisible")
        return []


async def _antennes(repo: RepositoryDep) -> tuple[list[dict[str, Any]], dict[str, float]]:
    try:
        antennes = await repo.backhaul_latest()
    except Exception:  # noqa: BLE001
        logger.exception("Antennes illisibles")
        return [], {}
    try:
        maxima = await repo.backhaul_capacity_max(hours=24)
    except Exception:  # noqa: BLE001
        maxima = {}
    return antennes, maxima


class LinkMedium(BaseModel):
    router: str = Field(min_length=1, max_length=128)
    interface: str = Field(min_length=1, max_length=128)
    medium: Literal["wired", "radio"]
    capacity_mbps: float | None = Field(default=None, gt=0, le=1_000_000)
    backhaul_name: str | None = Field(default=None, max_length=256)


@router.get("/capacity/media", summary="Wired or radio, link by link, and the antennas to pick")
async def list_media(repo: RepositoryDep, container: ContainerDep) -> dict[str, Any]:
    antennes, maxima = await _antennes(repo)
    return {
        "media": await _media(container),
        "antennas": [
            radio_state(b, nominal_fallback=maxima.get(str(b.get("name")))) for b in antennes
        ],
    }


@router.put("/capacity/media", summary="Declare a link wired (fixed capacity) or radio (live)")
async def set_medium(payload: LinkMedium, container: ContainerDep) -> dict[str, Any]:
    if container.link_media_repo is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    if payload.medium == "radio" and not payload.backhaul_name:
        raise HTTPException(
            status_code=422,
            detail="A radio link needs its antenna: pick the one whose live capacity it carries",
        )
    return await container.link_media_repo.set(
        router_name=payload.router,
        interface=payload.interface,
        medium=payload.medium,
        capacity_mbps=payload.capacity_mbps if payload.medium == "wired" else None,
        backhaul_name=payload.backhaul_name if payload.medium == "radio" else None,
        updated_by="ui",
    )


@router.delete(
    "/capacity/media/{router_name}/{interface:path}",
    summary="Back to automatic (smallest known capacity)",
    status_code=204,
)
async def delete_medium(router_name: str, interface: str, container: ContainerDep) -> None:
    if container.link_media_repo is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    await container.link_media_repo.delete(router_name, interface)
