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
    VlanSightingsRepository,
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


def _sightings(container: ContainerDep) -> VlanSightingsRepository | None:
    return container.sightings_repo


@router.get("/static-clients", summary="Inventaire des clients a IP fixe")
async def list_static_clients(
    container: ContainerDep,
    pop_name: Annotated[str | None, Query(max_length=128)] = None,
) -> list[dict[str, Any]]:
    """Fiches declarees, enrichies de leur derniere presence observee.

    La presence vient de la table ARP et ne modifie JAMAIS la fiche : c'est une
    lecture jointe a l'affichage. Une fiche sans presence n'est pas une erreur
    -- le client peut etre silencieux, ou joignable par un chemin que l'ARP du
    routeur ne montre pas.
    """
    fiches = await _require_repository(container).list_all(pop_name=pop_name)
    repo = _sightings(container)
    if repo is None:
        return fiches
    try:
        presence = await repo.presence()
    except Exception:  # noqa: BLE001 - l'inventaire doit rester lisible
        logger.exception("Presence des clients statiques illisible")
        return fiches
    for fiche in fiches:
        vu = presence.get(fiche["reference"])
        fiche["last_seen_at"] = vu["last_seen"] if vu else None
        fiche["seen_mac"] = vu["mac"] if vu else None
        fiche["seen_vlan_interface"] = vu["vlan_interface"] if vu else None
        fiche["seen_router"] = vu["router_name"] if vu else None
    return fiches


@router.get(
    "/static-clients/candidates",
    summary="Adresses detectees sur une VLAN routee, non declarees",
)
async def list_candidates(container: ContainerDep) -> dict[str, Any]:
    """Ce que la table ARP montre et que l'inventaire ne connait pas.

    A LIRE COMME UNE PISTE, PAS COMME UNE LISTE DE CLIENTS. Une entree ARP dit
    qu'une adresse a parle sur une VLAN sans PPPoE : rien de plus. Une
    imprimante, une camera ou l'equipement d'un autre operateur produisent le
    meme signal. Aucun de ces candidats n'est faconne, aucun n'a de plan, et
    aucun ne le sera tant qu'un humain n'aura pas saisi sa fiche.
    """
    repo = _sightings(container)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Detection indisponible (base non initialisee)",
        )
    if not container.settings.vlan_detect_enabled:
        return {"enabled": False, "candidates": [], "count": 0}
    lignes = await repo.candidates(
        limit=container.settings.vlan_candidate_limit,
        max_age_s=container.settings.vlan_sighting_retention_s,
    )
    return {"enabled": True, "candidates": lignes, "count": len(lignes)}


@router.get(
    "/static-clients/candidates/diagnostic",
    summary="Pourquoi un client sur VLAN est vu, ou ne l'est pas",
)
async def diagnose_candidates(
    container: ContainerDep,
    router_name: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    """Lit /ip/arp en direct et rend le motif de chaque ligne ecartee.

    A LIRE D'ABORD QUAND UN CLIENT MANQUE. La detection suppose que l'adressage
    du client est pose sur une interface de ``/interface/vlan``. Beaucoup de
    routeurs portent l'adresse sur un PONT en filtrage VLAN : la table ARP nomme
    alors ce pont, et le client est invisible. ``interfaces_hors_vlan`` le montre
    d'un coup d'oeil -- une interface qui y apparait avec plusieurs adresses est
    presque toujours la reponse.

    Lecture seule : trois commandes ``print``, rien n'est configure.
    """
    collecteurs = [c for c in container.registry.collectors if router_name in (None, c.name)]
    if not collecteurs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Aucun routeur collecte ne correspond a '{router_name}'. "
                f"Un routeur ecarte de la collecte n'est lu nulle part : "
                f"verifiez l'onglet Equipements."
            )
            if router_name
            else "Aucun routeur n'est collecte.",
        )

    rapports: list[dict[str, Any]] = []
    for collecteur in collecteurs:
        try:
            rapports.append(await collecteur.explain_vlan_clients())
        except Exception as exc:  # noqa: BLE001 - un routeur muet ne casse pas le reste
            rapports.append(
                {
                    "router": collecteur.name,
                    "pop_name": collecteur.config.effective_pop_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"enabled": container.settings.vlan_detect_enabled, "routers": rapports}


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
