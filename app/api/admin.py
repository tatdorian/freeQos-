"""Endpoints d'exploitation : etat du collecteur et declenchement manuel.

Le declenchement manuel ne fait que rejouer un cycle de LECTURE : il ne pousse
rien sur le reseau.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BaseModel

from app.api.deps import CollectionDep, ContainerDep, RepositoryDep, SchedulerDep
from app.services.tls import describe_tls

router = APIRouter(tags=["operations"])


class RttToggle(BaseModel):
    enabled: bool


@router.get("/rtt", summary="State of the latency probe (RTT)")
async def rtt_state(container: ContainerDep, collection: CollectionDep) -> dict[str, Any]:
    """La sonde RTT alimente RTT, QoO et bufferbloat de l'onglet Executif.

    Elle se pilote ici, depuis l'interface : plus besoin de RTT_ENABLED dans
    l'environnement une fois le drapeau amorce en base.
    """
    return {
        "enabled": collection.rtt_enabled,
        "env_default": container.settings.rtt_enabled,
        "interval_s": container.settings.rtt_interval_s,
        "batch_size": container.settings.rtt_batch_size,
    }


@router.get("/rtt/diagnose", summary="Run the latency probe by hand, with and without source")
async def rtt_diagnose(
    collection: CollectionDep,
    router_name: Annotated[str, Query(alias="router", description="Router that pings")],
    address: Annotated[str, Query(description="Client address to ping")],
) -> dict[str, Any]:
    """LA MEME SERIE QUE LA SONDE, a la demande, et la reponse BRUTE de RouterOS.

    Deux essais : depuis le loopback (comme la sonde), puis sans source. Quand
    tous les clients repondent « no reply », c'est ce qui dit pourquoi : chemin
    retour vers le loopback, pare-feu, ou droit 'test' manquant sur le compte.
    """
    import asyncio

    from app.collectors.mikrotik import ping_stats_from_rows

    collecteur = next((c for c in collection.collectors if c.name == router_name), None)
    if collecteur is None:
        raise HTTPException(status_code=404, detail=f"unknown router: {router_name}")
    source = await collecteur.ensure_loopback()
    essais: list[dict[str, Any]] = []
    for src in ([source] if source else []) + [None]:
        try:
            lignes = await asyncio.wait_for(
                asyncio.to_thread(collecteur._sonde.ping, address, 5, src, "200ms"),  # noqa: SLF001
                timeout=15,
            )
            stats = ping_stats_from_rows(lignes, 5)
            essais.append(
                {"source": src, "stats": stats.to_dict(), "raw": [dict(x) for x in lignes][:10]}
            )
        except Exception as exc:  # noqa: BLE001 - le diagnostic dit l'erreur
            essais.append({"source": src, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "router": router_name,
        "address": address,
        "loopback": source,
        "probe_skips_loopback": bool(getattr(collecteur, "_ping_sans_source", False)),
        "attempts": essais,
    }


@router.put("/rtt", summary="Turn the latency probe (RTT) on or off")
async def set_rtt(
    payload: RttToggle, container: ContainerDep, collection: CollectionDep
) -> dict[str, Any]:
    """Bascule la sonde sans redemarrage. Le job reste planifie : couper la sonde
    l'endort, la reactiver la relance au cycle suivant. Le compte de lecture doit
    posseder la policy ``test`` sur RouterOS pour que ``/ping`` reponde."""
    collection.rtt_enabled = payload.enabled
    if container.topology_repo is not None:
        from app.services.collection import FLAG_RTT

        await container.topology_repo.set_flag(
            FLAG_RTT, payload.enabled, updated_by="ui", reason="bascule depuis l'interface"
        )
    return {"enabled": collection.rtt_enabled}


@router.get("/latency", summary="Latency by segment: access, upstream, internet")
async def latency(container: ContainerDep, collection: CollectionDep) -> dict[str, Any]:
    """OU SE PERD LE TEMPS, routeur par routeur.

    Trois segments mesures depuis le MEME routeur, par la meme methode (une
    serie de pings dont on garde tout : mediane, extremes, gigue, perte) :

    - ``access``   : PoP -> ses abonnes, resume sur tous ceux mesures ;
    - ``gateway``  : PoP -> sa passerelle par defaut (le lien vers le coeur) ;
    - ``internet`` : PoP -> des cibles publiques (``LATENCY_INTERNET_TARGETS``).
    """
    from app.collectors.mikrotik import upstream_of
    from app.services.rtt import summarise

    settings = container.settings
    sonde = collection.rtt_prober
    chemins = collection.path_prober.snapshot() if collection.path_prober is not None else {}
    acces = sonde.readings_by_router() if sonde is not None else {}
    routeurs = []
    for collector in collection.collectors:
        nom = collector.name
        passerelle, interface = upstream_of(nom)
        segments = chemins.get(nom, {})
        routeurs.append(
            {
                "router": nom,
                "pop_name": collector.config.effective_pop_name,
                "role": str(collector.config.role),
                "upstream_gateway": passerelle,
                "upstream_interface": interface,
                "access": summarise(acces.get(nom, [])),
                "gateway": next(iter(segments.get("gateway") or []), None),
                "internet": segments.get("internet") or [],
            }
        )
    return {
        "enabled": collection.rtt_enabled,
        "method": {
            "count": settings.rtt_count,
            "interval_ms": settings.rtt_ping_interval_ms,
            "every_s": settings.rtt_interval_s,
            "reported": "median of the series (min, max, jitter and loss kept)",
            "source": "the PoP router itself (/ping from its loopback)",
            "internet_targets": list(settings.latency_internet_targets),
        },
        "routers": routeurs,
    }


@router.get("/latency/clients", summary="Latency by client: the experience each one lives")
async def latency_by_client(
    container: ContainerDep,
    collection: CollectionDep,
    repo: RepositoryDep,
    minutes: Annotated[int, Query(ge=5, le=10_080)] = 60,
) -> dict[str, Any]:
    """CHAQUE CLIENT, SA LATENCE. Mediane et p95 sur la periode, gigue et perte
    de la derniere serie, latence sous charge (bufferbloat) et score
    d'experience. Le pire ressenti en tete : c'est lui qu'on appelle d'abord."""
    from app.services.latency_clients import build_rows, summary

    latences = await repo.latency_by_subscriber(minutes=minutes)
    charge: dict[int, dict[str, Any]] = {}
    try:
        bloat = await repo.bufferbloat(minutes=minutes)
        for b in bloat.get("subscribers") or []:
            charge[int(b["subscriber_id"])] = b
    except Exception:  # noqa: BLE001 - sans charge, la latence a vide reste
        charge = {}
    sonde = collection.rtt_prober
    series: dict[int, dict[str, Any]] = {}
    if sonde is not None:
        for lat in latences:
            detail = sonde.detail(int(lat["subscriber_id"]))
            if detail:
                series[int(lat["subscriber_id"])] = detail
    lignes = build_rows(latences, charge, series)
    settings = container.settings
    return {
        "enabled": collection.rtt_enabled,
        "minutes": minutes,
        "method": {
            "count": settings.rtt_count,
            "interval_ms": settings.rtt_ping_interval_ms,
            "every_s": settings.rtt_interval_s,
            "source": "the client's own PoP router (/ping), under load and at rest",
        },
        "summary": summary(lignes),
        "clients": lignes,
    }


@router.get("/status", summary="State of the controller and its cycles")
async def status_view(
    container: ContainerDep,
    scheduler: SchedulerDep,
    collection: CollectionDep,
) -> dict[str, Any]:
    settings = container.settings
    return {
        "mode": "out-of-band (lecture seule)",
        "phase": 1,
        "enforcement_enabled": settings.enforcement_enabled,
        "scheduler_running": scheduler.running,
        "jobs": scheduler.status(),
        "last_results": {
            job: {
                "started_at": result.started_at,
                "duration_s": result.duration_s,
                "ok": result.ok,
                "items": result.items,
                "errors": result.errors,
            }
            for job, result in collection.last_results.items()
        },
        "routers": [
            {
                "name": collector.config.name,
                "host": collector.config.host,
                "port": collector.config.port,
                "role": collector.config.role.value,
                "pop": collector.config.effective_pop_name,
                "username": collector.config.username,
                "use_ssl": collector.config.use_ssl,
                # Posture TLS visible : une desactivation doit se voir.
                "tls": describe_tls(collector.config),
            }
            for collector in collection.collectors
        ],
        "backhauls": [
            {
                "name": backhaul.name,
                "pop": backhaul.pop_name,
                "uisp_device_id": backhaul.uisp_device_id,
                "nominal_capacity_mbps": backhaul.nominal_capacity_mbps,
            }
            for backhaul in collection.backhauls
        ],
        "providers": {
            "backhaul": settings.backhaul_provider,
            "plans": settings.plan_provider,
        },
        "rate_tracker": {
            "tracked_sessions": len(collection.rates),
            "resets_detected": collection.rates.resets_detected,
        },
    }


@router.get("/status/runs", summary="History of the collection cycles")
async def recent_runs(repo: RepositoryDep, limit: int = 20) -> list[dict[str, Any]]:
    return await repo.recent_runs(limit=min(max(limit, 1), 200))


@router.get("/status/counters", summary="Global counters of the reference data")
async def counters(repo: RepositoryDep) -> dict[str, Any]:
    return await repo.counters()


@router.post("/jobs/{job_name}/run", summary="Replay a collection cycle immediately")
async def run_job(
    scheduler: SchedulerDep,
    job_name: Annotated[str, Path(description="Job name, see /status")],
) -> dict[str, Any]:
    try:
        result = await scheduler.run_once(job_name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown job: {job_name} (available: {scheduler.job_names()})",
        ) from None
    if result is None:
        return {"job": job_name, "ok": False, "detail": "The job raised an exception"}
    return {
        "job": result.job,
        "ok": result.ok,
        "items": result.items,
        "duration_s": result.duration_s,
        "errors": result.errors,
    }
