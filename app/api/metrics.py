"""Endpoints de lecture des metriques collectees."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.deps import RepositoryDep, TimeRangeDep

router = APIRouter(tags=["metrics"])


@router.get("/pops", summary="Liste des PoPs")
async def list_pops(repo: RepositoryDep) -> list[dict[str, Any]]:
    return await repo.list_pops()


@router.get("/subscribers", summary="Liste des abonnes")
async def list_subscribers(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query(description="Filtre par PoP")] = None,
    search: Annotated[str | None, Query(description="Filtre sur le login PPPoE")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return await repo.list_subscribers(pop_id=pop_id, search=search, limit=limit, offset=offset)


@router.get("/subscribers/latest", summary="Dernier echantillon par abonne (top talkers)")
async def subscribers_latest(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    order_by: Annotated[Literal["total", "down", "up", "login"], Query()] = "total",
) -> list[dict[str, Any]]:
    return await repo.subscriber_latest(pop_id=pop_id, limit=limit, order_by=order_by)


@router.get("/subscribers/{subscriber_id}", summary="Fiche d'un abonne")
async def get_subscriber(
    repo: RepositoryDep,
    subscriber_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    subscriber = await repo.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Abonne inconnu")
    return subscriber


@router.get("/subscribers/{subscriber_id}/metrics", summary="Serie de debits d'un abonne")
async def subscriber_metrics(
    repo: RepositoryDep,
    window: TimeRangeDep,
    subscriber_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    subscriber = await repo.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Abonne inconnu")
    points = await repo.subscriber_metrics(
        subscriber_id,
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
    )
    return {
        "subscriber": subscriber,
        "start": window.start,
        "end": window.end,
        "bucket_seconds": window.bucket_seconds,
        # Rappel de convention : rx = upload abonne, tx = download abonne.
        "orientation": "rx=upload abonne, tx=download abonne (point de vue routeur)",
        "points": points,
    }


@router.get("/backhauls", summary="Liste des backhauls radio")
async def list_backhauls(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await repo.list_backhauls(pop_id=pop_id)


@router.get("/backhauls/latest", summary="Derniere capacite connue par backhaul")
async def backhauls_latest(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await repo.backhaul_latest(pop_id=pop_id)


@router.get("/backhauls/{backhaul_id}/metrics", summary="Serie de capacite d'un backhaul")
async def backhaul_metrics(
    repo: RepositoryDep,
    window: TimeRangeDep,
    backhaul_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    points = await repo.backhaul_metrics(
        backhaul_id,
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
    )
    return {
        "backhaul_id": backhaul_id,
        "start": window.start,
        "end": window.end,
        "bucket_seconds": window.bucket_seconds,
        "points": points,
    }
