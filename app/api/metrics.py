"""Endpoints de lecture des metriques collectees."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.deps import RepositoryDep, TimeRangeDep

router = APIRouter(tags=["metrics"])


@router.get("/pops", summary="Liste des PoPs")
async def list_pops(repo: RepositoryDep) -> list[dict[str, Any]]:
    return await repo.list_pops()


@router.delete("/pops/{pop_id}", summary="Retirer un PoP et ses donnees")
async def delete_pop(
    repo: RepositoryDep,
    pop_id: Annotated[int, Path(ge=1)],
    confirm: Annotated[
        bool, Query(description="Obligatoire : la suppression est definitive")
    ] = False,
) -> dict[str, Any]:
    """Supprime le PoP, ses abonnes, ses backhauls et leur historique.

    Retirer un routeur de l'inventaire ne suffit pas : ses donnees restent, ce
    qui est voulu. Cet appel est le menage explicite, et il est irreversible.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Suppression definitive du PoP, de ses abonnes et de tout leur "
                "historique de mesures : 'confirm' doit valoir true."
            ),
        )
    try:
        supprime = await repo.delete_pop(pop_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"deleted": True, "cascaded": supprime}


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
    search: Annotated[str | None, Query(description="Filtre sur le login PPPoE")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    order_by: Annotated[Literal["total", "down", "up", "login"], Query()] = "total",
) -> list[dict[str, Any]]:
    return await repo.subscriber_latest(
        pop_id=pop_id, search=search, limit=limit, order_by=order_by
    )


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
    # Bufferbloat de CET abonne sur la meme fenetre : latence a vide vs sous
    # charge. Calcule sur les echantillons bruts, pas sur les points agreges,
    # pour que la note reste juste quel que soit le bucket demande.
    minutes = max(5, round((window.end - window.start).total_seconds() / 60))
    bloat = await repo.bufferbloat(minutes=minutes, subscriber_id=subscriber_id)
    return {
        "subscriber": subscriber,
        "start": window.start,
        "end": window.end,
        "bucket_seconds": window.bucket_seconds,
        # Rappel de convention : rx = upload abonne, tx = download abonne.
        "orientation": "rx=upload abonne, tx=download abonne (point de vue routeur)",
        "points": points,
        "bufferbloat": bloat["subscribers"][0] if bloat["subscribers"] else None,
    }


@router.get("/bufferbloat", summary="Note de bufferbloat (latence sous charge) par abonne")
async def bufferbloat(
    repo: RepositoryDep,
    minutes: Annotated[int, Query(ge=5, le=60 * 24 * 7, description="Fenetre d'observation")] = 60,
    pop_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """Le bufferbloat est la latence AJOUTEE quand le lien se remplit.

    Il se lit en correlant RTT et debit deja collectes : rien de nouveau a
    mesurer, juste a rapprocher. Un abonne sans charge sur la fenetre reste sans
    note (compte dans ``indeterminate``) plutot que d'en recevoir une flatteuse.
    """
    return await repo.bufferbloat(minutes=minutes, pop_id=pop_id)


@router.get("/heatmap", summary="Heatmap executif : QoE / RTT / utilisation dans le temps")
async def heatmap(
    repo: RepositoryDep,
    minutes: Annotated[int, Query(ge=5, le=60 * 24, description="Fenetre d'observation")] = 15,
    buckets: Annotated[int, Query(ge=5, le=120, description="Nombre de colonnes")] = 20,
) -> dict[str, Any]:
    """Bandes de cellules colorees facon LibreQoS. La ligne des retransmissions
    TCP est presente mais marquee indisponible : hors-bande, on ne l'invente pas."""
    return await repo.heatmap(minutes=minutes, buckets=buckets)


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


# --------------------------------------------------------------------------
# Vues d'ensemble consommees par le tableau de bord
# --------------------------------------------------------------------------


@router.get("/overview", summary="Chiffres de tete du tableau de bord")
async def overview(repo: RepositoryDep) -> dict[str, Any]:
    return await repo.overview()


@router.get("/throughput", summary="Debit agrege du reseau dans le temps")
async def throughput(
    repo: RepositoryDep,
    window: TimeRangeDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    points = await repo.throughput_series(
        start=window.start,
        end=window.end,
        bucket_seconds=window.bucket_seconds,
        pop_id=pop_id,
    )
    return {
        "start": window.start,
        "end": window.end,
        "bucket_seconds": window.bucket_seconds,
        "orientation": "rx=upload abonnes, tx=download abonnes (point de vue routeur)",
        "points": points,
    }


@router.get("/network/tree", summary="Arbre PoP -> backhauls, capacite et charge")
async def network_tree(repo: RepositoryDep) -> list[dict[str, Any]]:
    return await repo.network_tree()
