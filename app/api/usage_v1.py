"""API publique de consommation -- pendant de ``/model/v1``.

CE QU'ELLE REND, ET D'OU CA VIENT
---------------------------------
Des OCTETS par service et par periode. C'est la question que pose un systeme de
facturation ("combien cette ligne a-t-elle consomme le mois dernier") et un
portail client ("ou en suis-je de mon quota").

La source est NetFlow, et c'est un choix, pas un defaut. Les compteurs d'une
file RouterOS comptent ce qui traverse CETTE file : ils remettent a zero a
chaque reconnexion PPPoE, ils ne survivent pas a un redemarrage du routeur, et
ils ne disent rien du trafic d'un client dont la file n'existe pas encore. Le
flux exporte, lui, est date, resiste au redemarrage du collecteur, et couvre
tout ce qui passe -- y compris ce qu'on ne bride pas.

Donc : sans exporteur NetFlow declare, cette API rend des zeros. Elle le dit
dans ``source`` plutot que de laisser croire a un reseau silencieux.

UN SEUL POINT DE MESURE EST LU. Le meme octet est exporte par le PoP et par la
sortie internet ; les additionner doublerait chaque facture. Le point retenu est
``NETFLOW_ACCOUNTING_VANTAGE``, surchargeable par requete.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.auth import ReadDep
from app.api.deps import ContainerDep
from app.db.flows_repo import FlowsRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/usage/v1", tags=["api publique (consommation)"])

Bucket = Literal["total", "hour", "day", "month"]


def _flows(container: ContainerDep) -> FlowsRepository:
    if container.flows_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mesures de trafic indisponibles (base non initialisee)",
        )
    return container.flows_repo


def _window(start: datetime | None, end: datetime | None, days: int) -> tuple[datetime, datetime]:
    fin = end or datetime.now(tz=UTC)
    debut = start or (fin - timedelta(days=days))
    if debut.tzinfo is None:
        debut = debut.replace(tzinfo=UTC)
    if fin.tzinfo is None:
        fin = fin.replace(tzinfo=UTC)
    if debut >= fin:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'start' doit preceder 'end'",
        )
    return debut, fin


def _vantage(container: ContainerDep, demande: str | None) -> str:
    if demande:
        return demande
    return container.netflow.accounting_vantage if container.netflow else "edge"


def _render(lignes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": ligne["id"],
            "account": ligne.get("account"),
            "package": ligne.get("package"),
            "period_start": ligne.get("period_start"),
            "down_bytes": int(ligne.get("down_bytes") or 0),
            "up_bytes": int(ligne.get("up_bytes") or 0),
            "total_bytes": int(ligne.get("down_bytes") or 0) + int(ligne.get("up_bytes") or 0),
        }
        for ligne in lignes
    ]


@router.get("", summary="Ce que cette API rend")
async def index(caller: ReadDep) -> dict[str, Any]:
    return {
        "api": "freeqos-usage",
        "version": "v1",
        "unit": "bytes",
        "source": "netflow",
        "buckets": ["total", "hour", "day", "month"],
        "key": caller.prefix,
    }


@router.get("/services", summary="Consommation de tous les services")
async def usage_all(
    container: ContainerDep,
    caller: ReadDep,
    start: Annotated[datetime | None, Query(description="Debut (ISO 8601, UTC)")] = None,
    end: Annotated[datetime | None, Query(description="Fin (ISO 8601, UTC)")] = None,
    days: Annotated[int, Query(ge=1, le=366, description="Fenetre si 'start' absent")] = 30,
    bucket: Annotated[Bucket, Query(description="Decoupage de la periode")] = "total",
    vantage: Annotated[str | None, Query(description="edge | pop")] = None,
) -> dict[str, Any]:
    debut, fin = _window(start, end, days)
    point = _vantage(container, vantage)
    lignes = await _flows(container).usage(
        start=debut,
        end=fin,
        vantage=point,
        bucket=None if bucket == "total" else bucket,
    )
    return {
        "start": debut,
        "end": fin,
        "bucket": bucket,
        "vantage": point,
        "unit": "bytes",
        "source": "netflow",
        "services": _render(lignes),
    }


@router.get("/services/{service_id}", summary="Consommation d'un service")
async def usage_one(
    service_id: Annotated[str, Path(min_length=1, max_length=128)],
    container: ContainerDep,
    caller: ReadDep,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    bucket: Annotated[Bucket, Query()] = "day",
    vantage: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    debut, fin = _window(start, end, days)
    point = _vantage(container, vantage)
    lignes = await _flows(container).usage(
        start=debut,
        end=fin,
        vantage=point,
        bucket=None if bucket == "total" else bucket,
        service_id=service_id,
    )
    rendu = _render(lignes)
    return {
        "id": service_id,
        "start": debut,
        "end": fin,
        "bucket": bucket,
        "vantage": point,
        "unit": "bytes",
        "source": "netflow",
        "down_bytes": sum(ligne["down_bytes"] for ligne in rendu),
        "up_bytes": sum(ligne["up_bytes"] for ligne in rendu),
        "periods": rendu,
    }
