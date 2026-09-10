"""Endpoints d'exploitation : etat du collecteur et declenchement manuel.

Le declenchement manuel ne fait que rejouer un cycle de LECTURE : il ne pousse
rien sur le reseau.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel

from app.api.deps import CollectionDep, ContainerDep, RepositoryDep, SchedulerDep

router = APIRouter(tags=["exploitation"])


class RttToggle(BaseModel):
    enabled: bool


@router.get("/rtt", summary="Etat de la sonde de latence (RTT)")
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


@router.put("/rtt", summary="Activer ou couper la sonde de latence (RTT)")
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


@router.get("/status", summary="Etat du controleur et de ses cycles")
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


@router.get("/status/runs", summary="Historique des cycles de collecte")
async def recent_runs(repo: RepositoryDep, limit: int = 20) -> list[dict[str, Any]]:
    return await repo.recent_runs(limit=min(max(limit, 1), 200))


@router.get("/status/counters", summary="Compteurs globaux du referentiel")
async def counters(repo: RepositoryDep) -> dict[str, Any]:
    return await repo.counters()


@router.post("/jobs/{job_name}/run", summary="Rejoue un cycle de collecte immediatement")
async def run_job(
    scheduler: SchedulerDep,
    job_name: Annotated[str, Path(description="Nom du job, cf. /status")],
) -> dict[str, Any]:
    try:
        result = await scheduler.run_once(job_name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job inconnu : {job_name} (disponibles : {scheduler.job_names()})",
        ) from None
    if result is None:
        return {"job": job_name, "ok": False, "detail": "Le job a leve une exception"}
    return {
        "job": result.job,
        "ok": result.ok,
        "items": result.items,
        "duration_s": result.duration_s,
        "errors": result.errors,
    }
