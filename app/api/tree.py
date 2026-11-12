"""Arbre reseau modulaire : placer les clients, lier une case a un client.

L'arbre reste AUTOMATIQUE (cf. ``app.services.tree_placement``) ; ces routes
enregistrent seulement ce que l'exploitant corrige, et chaque correction se
defait (« back to automatic »).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, acteur
from app.db.tree_repo import TreeRepository
from app.services.tree_placement import place_clients

logger = logging.getLogger(__name__)
router = APIRouter(tags=["topology"])


class PlaceInput(BaseModel):
    logins: list[str] = Field(min_length=1, max_length=500)
    node_key: str = Field(min_length=1, max_length=512)


class LinkInput(BaseModel):
    node_key: str = Field(min_length=1, max_length=512)
    #: Vide : « cette case n'est pas un client » (refuse le lien par le nom).
    login: str = Field(default="", max_length=128)


def _repo(container: ContainerDep) -> TreeRepository:
    repo = getattr(container, "tree_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not initialised"
        )
    return repo  # type: ignore[no-any-return]


async def placements_for(
    container: Any,
    visible: list[dict[str, Any]],
    hidden: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ou pend chaque client de la liste. Ne leve jamais : un arbre sans
    placement vaut mieux que pas d'arbre."""
    vide: dict[str, Any] = {"placements": {}, "client_nodes": {}}
    metrics = getattr(container, "repository", None)
    if metrics is None:
        return vide
    try:
        clients = await metrics.subscriber_latest(
            limit=5000, order_by="login", include_unmeasured=True
        )
        repo_topo = getattr(container, "topology_repo", None)
        attachments = await repo_topo.attachments() if repo_topo is not None else {}
        sites = await metrics.pop_sites() if hasattr(metrics, "pop_sites") else []
        arbre = getattr(container, "tree_repo", None)
        places = await arbre.places() if arbre is not None else {}
        liens_manuels = await arbre.links() if arbre is not None else {}
        noms: dict[str, list[str]] = {}
        fiches = getattr(container, "client_profiles_repo", None)
        if fiches is not None:
            for fiche in await fiches.list_all():
                if fiche.get("display_name"):
                    noms.setdefault(str(fiche["login"]), []).append(str(fiche["display_name"]))
        fixes = getattr(container, "static_clients_repo", None)
        if fixes is not None:
            for fixe in await fixes.list_all():
                if fixe.get("label"):
                    noms.setdefault(str(fixe["reference"]), []).append(str(fixe["label"]))
    except Exception:  # noqa: BLE001
        logger.exception("Placement des clients dans l'arbre impossible")
        return vide
    return place_clients(
        visible=visible,
        hidden=hidden,
        links=links,
        clients=clients,
        attachments=attachments,
        pop_sites=sites,
        manual_places=places,
        manual_links=liens_manuels,
        names=noms,
    )


@router.put("/topology/client-places", summary="Hang clients under a box of the tree")
async def put_places(
    payload: PlaceInput, request: Request, container: ContainerDep
) -> dict[str, Any]:
    logins = list(dict.fromkeys(x.strip() for x in payload.logins if x.strip()))
    await _repo(container).set_place(logins, payload.node_key, updated_by=acteur(request))
    return {"placed": logins, "node_key": payload.node_key}


@router.delete(
    "/topology/client-places/{login}", summary="A client goes back to its automatic place"
)
async def delete_place(
    login: Annotated[str, Path(min_length=1, max_length=128)], container: ContainerDep
) -> dict[str, Any]:
    return {"login": login, "removed": await _repo(container).clear_place(login)}


@router.put("/topology/client-links", summary="Say which client a box is (or that it is none)")
async def put_link(payload: LinkInput, request: Request, container: ContainerDep) -> dict[str, Any]:
    login = payload.login.strip()
    await _repo(container).set_link(payload.node_key, login, updated_by=acteur(request))
    return {"node_key": payload.node_key, "login": login or None}


@router.delete(
    "/topology/client-links/{node_key:path}", summary="Back to the automatic link (by name)"
)
async def delete_link(node_key: str, container: ContainerDep) -> dict[str, Any]:
    return {"node_key": node_key, "removed": await _repo(container).clear_link(node_key)}
