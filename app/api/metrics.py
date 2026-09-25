"""Endpoints de lecture des metriques collectees."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.deps import CollectionDep, ContainerDep, RepositoryDep, TimeRangeDep

router = APIRouter(tags=["metrics"])


@router.get("/pops", summary="List of PoPs")
async def list_pops(repo: RepositoryDep) -> list[dict[str, Any]]:
    return await repo.list_pops()


@router.delete("/pops/{pop_id}", summary="Remove a PoP and its data")
async def delete_pop(
    repo: RepositoryDep,
    pop_id: Annotated[int, Path(ge=1)],
    confirm: Annotated[bool, Query(description="Required: the deletion is permanent")] = False,
) -> dict[str, Any]:
    """Supprime le PoP, ses abonnes, ses backhauls et leur historique.

    Retirer un routeur de l'inventaire ne suffit pas : ses donnees restent, ce
    qui est voulu. Cet appel est le menage explicite, et il est irreversible.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Permanent deletion of the PoP, its subscribers and all their "
                "measurement history: 'confirm' must be true."
            ),
        )
    try:
        supprime = await repo.delete_pop(pop_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"deleted": True, "cascaded": supprime}


@router.get("/subscribers", summary="List of subscribers")
async def list_subscribers(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query(description="Filter by PoP")] = None,
    search: Annotated[str | None, Query(description="Filter on the subscriber identifier")] = None,
    kind: Annotated[
        Literal["pppoe", "static"] | None,
        Query(description="Filter by kind: PPPoE subscriber or static-IP client"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return await repo.list_subscribers(
        pop_id=pop_id, search=search, kind=kind, limit=limit, offset=offset
    )


@router.get("/subscribers/latest", summary="The subscribers of a PoP and their last sample")
async def subscribers_latest(
    repo: RepositoryDep,
    container: ContainerDep,
    pop_id: Annotated[int | None, Query()] = None,
    search: Annotated[str | None, Query(description="Filter on the subscriber identifier")] = None,
    kind: Annotated[
        Literal["pppoe", "static"] | None,
        Query(description="Filter by kind: PPPoE subscriber or static-IP client"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    order_by: Annotated[Literal["total", "down", "up", "login"], Query()] = "total",
    include_unmeasured: Annotated[
        bool,
        Query(
            description=(
                "Inclure les abonnes declares qui n'ont AUCUNE mesure : jamais "
                "connectes, PoP plus collecte, ou simplement hors ligne depuis "
                "l'origine. Leurs debits sortent a null, jamais a zero."
            )
        ),
    ] = False,
) -> list[dict[str, Any]]:
    """L'effectif d'un PoP, avec la derniere mesure de chacun quand elle existe.

    Par defaut, seuls les abonnes MESURES sont rendus : c'est ce que demande un
    classement par debit. ``include_unmeasured=true`` rend tout l'effectif --
    la liste des abonnes qu'il y a sur le PoP, y compris ceux dont on n'a
    encore rien vu passer. Un abonne facture qui n'apparait nulle part est
    indiscernable d'un abonne qui n'existe pas.
    """
    lignes = await repo.subscriber_latest(
        pop_id=pop_id,
        search=search,
        kind=kind,
        limit=limit,
        order_by=order_by,
        include_unmeasured=include_unmeasured,
    )
    # La SERIE derriere la latence affichee : mediane, extremes, gigue, perte,
    # age de la mesure. Un chiffre seul ne dit pas s'il est stable.
    collection = getattr(container, "collection", None)
    sonde = getattr(collection, "rtt_prober", None) if collection is not None else None
    if sonde is not None:
        for ligne in lignes:
            ligne["rtt_detail"] = sonde.detail(int(ligne["subscriber_id"]))
    return lignes


@router.get("/subscribers/{subscriber_id}", summary="Record of one subscriber")
async def get_subscriber(
    repo: RepositoryDep,
    subscriber_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    subscriber = await repo.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown subscriber")
    return subscriber


@router.get("/subscribers/{subscriber_id}/metrics", summary="Throughput series of a subscriber")
async def subscriber_metrics(
    repo: RepositoryDep,
    window: TimeRangeDep,
    subscriber_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    subscriber = await repo.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown subscriber")
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


@router.get("/bufferbloat", summary="Bufferbloat grade (latency under load) per subscriber")
async def bufferbloat(
    repo: RepositoryDep,
    collection: CollectionDep,
    minutes: Annotated[int, Query(ge=5, le=60 * 24 * 7, description="Observation window")] = 60,
    pop_id: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """Le bufferbloat est la latence AJOUTEE quand le lien se remplit.

    Il se lit en correlant RTT et debit deja collectes : rien de nouveau a
    mesurer, juste a rapprocher. Un abonne sans charge sur la fenetre reste sans
    note (compte dans ``indeterminate``) plutot que d'en recevoir une flatteuse.

    LA REPONSE DIT SI LA SONDE TOURNE. Sans RTT, cette note ne peut pas exister :
    la moitie de la correlation manque. La sonde etant coupee par defaut (elle
    coute du CPU aux routeurs), un tableau vide etait indiscernable d'un reseau
    parfaitement sain -- c'est le pire des deux messages possibles. On l'annonce
    donc explicitement plutot que de laisser deviner.
    """
    resultat = await repo.bufferbloat(minutes=minutes, pop_id=pop_id)
    resultat["rtt_enabled"] = collection.rtt_enabled
    if not collection.rtt_enabled:
        resultat["unavailable_reason"] = (
            "La sonde de latence est coupee : sans RTT, le bufferbloat et le score "
            "de QoE ne peuvent pas etre calcules. Activez-la dans l'onglet Executif "
            "(case 'Sonde RTT'), ou via PUT /api/v1/rtt."
        )
    return resultat


@router.get("/heatmap", summary="Executive heatmap: QoE / RTT / utilisation over time")
async def heatmap(
    repo: RepositoryDep,
    minutes: Annotated[int, Query(ge=5, le=60 * 24, description="Observation window")] = 15,
    buckets: Annotated[int, Query(ge=5, le=120, description="Number of columns")] = 20,
) -> dict[str, Any]:
    """Bandes de cellules colorees facon LibreQoS. La ligne des retransmissions
    TCP est presente mais marquee indisponible : hors-bande, on ne l'invente pas."""
    return await repo.heatmap(minutes=minutes, buckets=buckets)


@router.get("/backhauls", summary="List of radio backhauls")
async def list_backhauls(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await repo.list_backhauls(pop_id=pop_id)


@router.get("/backhauls/latest", summary="Last known capacity per backhaul")
async def backhauls_latest(
    repo: RepositoryDep,
    pop_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    return await repo.backhaul_latest(pop_id=pop_id)


@router.get("/backhauls/{backhaul_id}/metrics", summary="Capacity series of a backhaul")
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


@router.get("/overview", summary="Headline figures of the dashboard")
async def overview(repo: RepositoryDep) -> dict[str, Any]:
    return await repo.overview()


@router.get("/throughput", summary="Aggregate network throughput over time")
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


@router.get("/network/tree", summary="PoP tree -> backhauls, capacity and load")
async def network_tree(repo: RepositoryDep) -> list[dict[str, Any]]:
    return await repo.network_tree()


@router.get("/ports/live", summary="Every router port with its current throughput")
async def ports_live(repo: RepositoryDep, collection: CollectionDep) -> dict[str, Any]:
    """OU PASSE LE TRAFIC EN CE MOMENT, port par port, tous routeurs confondus.

    La courbe du reseau additionne les SESSIONS D'ABONNES. Un trafic qui n'en
    traverse aucune -- un test de debit lance depuis un CPE ou entre deux
    routeurs, la gestion d'un equipement -- n'y figure pas, alors que les ports
    le comptent. Cette vue les montre tous, que la decouverte les ait relies a
    un lien de l'arbre ou non, avec l'etat des cycles de collecte : un chiffre
    absent se lit alors "rien ne passe" OU "la mesure est en panne", jamais
    l'un pour l'autre.
    """
    from app.collectors.mikrotik import upstream_of
    from app.services.collection import JOB_LINKS, JOB_RTT, JOB_SUBSCRIBERS

    ports = await repo.ports_live()
    amonts = {c.name: upstream_of(c.name)[1] for c in collection.collectors}
    for port in ports:
        port["upstream"] = bool(amonts.get(port["router_name"])) and (
            amonts.get(port["router_name"]) == port["interface"]
        )
    maintenant = datetime.now(tz=UTC)
    cycles: dict[str, dict[str, Any] | None] = {}
    for job in (JOB_SUBSCRIBERS, JOB_LINKS, JOB_RTT):
        resultat = collection.last_results.get(job)
        if resultat is None:
            cycles[job] = None
            continue
        cycles[job] = {
            "ok": resultat.ok,
            "age_s": round((maintenant - resultat.started_at).total_seconds(), 1),
            "duration_s": round(resultat.duration_s, 2),
            "items": resultat.items,
            "errors": list(resultat.errors)[:5],
        }
    return {
        "ports": ports,
        "upstream": amonts,
        "cycles": cycles,
        "routers": [c.name for c in collection.collectors],
    }
