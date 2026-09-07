"""Endpoints d'exploitation : etat du collecteur et declenchement manuel.

Le declenchement manuel ne fait que rejouer un cycle de LECTURE : il ne pousse
rien sur le reseau.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status

from app.api.deps import CollectionDep, ContainerDep, RepositoryDep, SchedulerDep

router = APIRouter(tags=["exploitation"])


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
