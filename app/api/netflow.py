"""Trafic mesure par NetFlow, et declaration des exporteurs.

Ces routes servent l'interface d'exploitation (meme origine). L'API EXTERNE,
celle qu'un systeme tiers appelle, vit sous ``/model/v1`` et ``/usage/v1`` et
demande une cle.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import ContainerDep, TimeRangeDep
from app.db.flows_repo import (
    ExporterNotFoundError,
    FlowsRepository,
    NetflowExportersRepository,
)
from app.services.netflow_service import NetflowService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trafic (netflow)"])

Vantage = Literal["edge", "pop", "unknown"]


class ExporterInput(BaseModel):
    address: str = Field(
        min_length=1,
        max_length=64,
        description="Adresse IP depuis laquelle l'equipement exporte ses flux",
    )
    name: str | None = Field(default=None, max_length=128)
    vantage: Vantage = Field(
        default="pop",
        description=(
            "'edge' = en amont du coeur, a la sortie internet ; 'pop' = au PoP. "
            "Le meme octet est vu aux deux endroits : le comptage n'en retient "
            "qu'un seul."
        ),
    )
    pop_name: str | None = Field(default=None, max_length=128)
    sampling_rate: int = Field(
        default=1,
        ge=1,
        le=100_000,
        description=(
            "Taux d'echantillonnage configure sur l'equipement (1 = tout). Les "
            "octets lus sont multiplies par ce facteur."
        ),
    )
    enabled: bool = True
    note: str | None = Field(default=None, max_length=512)

    @field_validator("address")
    @classmethod
    def _valide(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise ValueError(f"adresse invalide : {value}") from exc


class ExporterUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    vantage: Vantage | None = None
    pop_name: str | None = Field(default=None, max_length=128)
    sampling_rate: int | None = Field(default=None, ge=1, le=100_000)
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=512)


def _service(container: ContainerDep) -> NetflowService:
    if container.netflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Collecteur NetFlow indisponible",
        )
    return container.netflow


def _flows(container: ContainerDep) -> FlowsRepository:
    if container.flows_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mesures de trafic indisponibles (base non initialisee)",
        )
    return container.flows_repo


def _exporters(container: ContainerDep) -> NetflowExportersRepository:
    if container.exporters_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Declaration des exporteurs indisponible (base non initialisee)",
        )
    return container.exporters_repo


@router.get("/netflow/status", summary="Etat du collecteur NetFlow")
async def netflow_status(container: ContainerDep) -> dict[str, Any]:
    return _service(container).status()


@router.get("/netflow/exporters", summary="Equipements qui exportent des flux")
async def list_exporters(container: ContainerDep) -> list[dict[str, Any]]:
    return list(await _exporters(container).list_all())


@router.post(
    "/netflow/exporters",
    status_code=status.HTTP_201_CREATED,
    summary="Declarer un exporteur (ou corriger sa declaration)",
)
async def declare_exporter(payload: ExporterInput, container: ContainerDep) -> dict[str, Any]:
    fiche = await _exporters(container).declare(payload.model_dump())
    # Sans ce rechargement, la declaration n'aurait d'effet qu'au prochain
    # flush : les flux de la minute en cours resteraient comptes en 'unknown'.
    await _service(container).refresh_exporters()
    return dict(fiche)


@router.patch("/netflow/exporters/{exporter_id}", summary="Modifier un exporteur")
async def update_exporter(
    exporter_id: Annotated[int, Path(ge=1)],
    payload: ExporterUpdate,
    container: ContainerDep,
) -> dict[str, Any]:
    champs = payload.model_dump(exclude_unset=True, exclude_none=True)
    try:
        fiche = await _exporters(container).update(exporter_id, champs)
    except ExporterNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _service(container).refresh_exporters()
    return dict(fiche)


@router.delete(
    "/netflow/exporters/{exporter_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirer un exporteur de la liste",
)
async def delete_exporter(exporter_id: Annotated[int, Path(ge=1)], container: ContainerDep) -> None:
    try:
        await _exporters(container).delete(exporter_id)
    except ExporterNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _service(container).refresh_exporters()


@router.get("/netflow/top", summary="Qui consomme, et combien")
async def top_talkers(
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 31)] = 60,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    vantage: Annotated[Vantage | None, Query()] = None,
) -> dict[str, Any]:
    service = _service(container)
    point = vantage or service.accounting_vantage
    repo = _flows(container)
    return {
        "vantage": point,
        "minutes": minutes,
        "totals": await repo.totals(minutes=minutes, vantage=point),
        "subscribers": await repo.top_subscribers(minutes=minutes, vantage=point, limit=limit),
    }


@router.get("/netflow/applications", summary="Repartition du trafic par usage")
async def applications(
    container: ContainerDep,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 31)] = 60,
    subscriber_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
) -> list[dict[str, Any]]:
    return await _flows(container).applications(
        minutes=minutes, subscriber_id=subscriber_id, limit=limit
    )


@router.get(
    "/netflow/subscribers/{subscriber_id}/series",
    summary="Volume dans le temps pour un abonne",
)
async def subscriber_series(
    subscriber_id: Annotated[int, Path(ge=1)],
    container: ContainerDep,
    window: TimeRangeDep,
    vantage: Annotated[Vantage | None, Query()] = None,
) -> list[dict[str, Any]]:
    point = vantage or _service(container).accounting_vantage
    return await _flows(container).subscriber_series(
        subscriber_id=subscriber_id,
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
        vantage=point,
    )


@router.get(
    "/netflow/hosts",
    summary="Adresses vues dans les flux et rattachees a aucune fiche",
)
async def unmatched_hosts(
    container: ContainerDep,
    vlan: Annotated[int | None, Query(ge=1, le=4094)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    max_age_s: Annotated[float, Query(ge=60, le=2_592_000)] = 86_400.0,
) -> dict[str, Any]:
    """AIDE A LA SAISIE, ET RIEN D'AUTRE.

    Une adresse qui parle n'est pas un client. Une imprimante, une camera, un
    equipement d'un autre operateur laissent exactement la meme trace, et rien
    dans un flux ne dit quel debit a ete vendu. Aucune ligne d'ici ne devient
    une fiche toute seule : un humain la declare dans ``/static-clients``, ou
    elle expire.
    """
    repo = _flows(container)
    return {
        "hosts": await repo.hosts(vlan_id=vlan, limit=limit, max_age_s=max_age_s),
        "vlans": await repo.vlans(max_age_s=max_age_s),
    }


@router.post("/netflow/flush", summary="Ecrire la fenetre en cours tout de suite")
async def flush_now(container: ContainerDep) -> dict[str, Any]:
    service = _service(container)
    ecrites = await service.flush()
    return {"written": ecrites, "status": service.status()}
