"""Inventaire des clients a IP fixe, gere depuis l'interface.

Ces endpoints ecrivent une DECLARATION en base, jamais sur un equipement. Ce
que l'operateur saisit ici devient, au cycle suivant, un abonne comme un autre :
meme table, meme planification, meme reconciliation que les abonnes PPPoE.

Il n'y a volontairement aucune decouverte automatique derriere. Un client a IP
fixe n'ouvre pas de session et n'a pas d'attribut RADIUS : rien sur le reseau ne
dit qu'une adresse appartient a tel client ni quel debit il a souscrit. La
saisie manuelle n'est pas un pis-aller, c'est la seule source qui existe.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.db.static_clients_repo import (
    DuplicateStaticClientError,
    InvalidStaticClientError,
    StaticClientNotFoundError,
    StaticClientsRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["clients statiques"])


class StaticClientInput(BaseModel):
    reference: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Identite stable du client, reprise telle quelle comme identifiant "
            "d'abonne. Choisir une reference qui ne changera pas : y encoder "
            "l'IP ou le VLAN ferait perdre ses surcharges et son historique au "
            "premier demenagement."
        ),
    )
    pop_name: str = Field(min_length=1, max_length=128)
    address: str = Field(
        min_length=1,
        max_length=64,
        description="IP fixe (10.0.0.5) ou sous-reseau attribue au client (10.0.0.0/29)",
    )
    label: str | None = Field(default=None, max_length=128)
    vlan: int | None = Field(default=None, ge=1, le=4094)
    sector_key: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Cle du noeud de topologie sous lequel rattacher le client. "
            "Declaratif : aucun caller-id n'existe pour ces clients."
        ),
    )
    plan_down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    plan_up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=512)


class StaticClientUpdate(BaseModel):
    """Mise a jour partielle : les champs omis restent inchanges."""

    reference: str | None = Field(default=None, min_length=1, max_length=64)
    pop_name: str | None = Field(default=None, min_length=1, max_length=128)
    address: str | None = Field(default=None, min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    vlan: int | None = Field(default=None, ge=1, le=4094)
    sector_key: str | None = Field(default=None, max_length=128)
    plan_down_mbps: float | None = Field(default=None, gt=0, le=100_000)
    plan_up_mbps: float | None = Field(default=None, gt=0, le=100_000)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=512)


def _require_repository(container: ContainerDep) -> StaticClientsRepository:
    if container.static_clients_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inventaire des clients statiques indisponible (base non initialisee)",
        )
    return container.static_clients_repo


@router.get("/static-clients", summary="Inventaire des clients a IP fixe")
async def list_static_clients(
    container: ContainerDep,
    pop_name: Annotated[str | None, Query(max_length=128)] = None,
) -> list[dict[str, Any]]:
    return await _require_repository(container).list_all(pop_name=pop_name)


@router.post(
    "/static-clients",
    status_code=status.HTTP_201_CREATED,
    summary="Declarer un client a IP fixe",
)
async def create_static_client(
    payload: StaticClientInput, container: ContainerDep
) -> dict[str, Any]:
    repository = _require_repository(container)
    try:
        created = await repository.create(payload.model_dump())
    except DuplicateStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info("Client statique '%s' declare depuis l'interface", created["reference"])
    return created


@router.patch("/static-clients/{client_id}", summary="Modifier un client a IP fixe")
async def update_static_client(
    payload: StaticClientUpdate,
    container: ContainerDep,
    client_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    repository = _require_repository(container)
    try:
        return await repository.update(client_id, payload.model_dump(exclude_unset=True))
    except StaticClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DuplicateStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidStaticClientError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete(
    "/static-clients/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirer un client de l'inventaire",
)
async def delete_static_client(
    container: ContainerDep,
    client_id: Annotated[int, Path(ge=1)],
) -> None:
    """Retire la fiche. L'historique de mesures du client est CONSERVE.

    Sa file tombera au plan suivant, faute de cible declaree : c'est la
    consequence normale, pas un effet de bord.
    """
    repository = _require_repository(container)
    try:
        await repository.delete(client_id)
    except StaticClientNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
