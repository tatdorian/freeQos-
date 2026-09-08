"""Sonde de vie et sonde de disponibilite.

/health repond tant que le processus tourne (liveness). /health/ready verifie la
base et la fraicheur des cycles de collecte : c'est cette sonde qui doit piloter
une eventuelle rotation de conteneur, pas la premiere.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": __version__,
        "ts": datetime.now(tz=UTC).isoformat(),
    }


@router.get("/health/ready", summary="Readiness (base + fraicheur de la collecte)")
async def readiness(container: ContainerDep, response: Response) -> dict[str, object]:
    db_ok = await container.database.ping()
    settings = container.settings

    jobs = container.scheduler.status()
    stale: list[str] = []
    for job in jobs:
        since = job["seconds_since_last_run"]
        if since is None:
            continue
        # Un job est considere en retard au-dela de trois periodes : en dessous,
        # un simple pic de latence declencherait de fausses alertes.
        if since > job["interval_s"] * 3:
            stale.append(job["job"])

    failing = [job["job"] for job in jobs if job["last_ok"] is False]
    ready = db_ok and not stale
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "timescaledb": container.database.timescale_available,
        "scheduler_running": container.scheduler.running,
        # Deux populations distinctes : ce qui est declare dans l'inventaire
        # fichier, et ce qui est reellement interroge (fichier + base, moins les
        # routeurs ecartes faute de secret lisible).
        "routers_in_file": len(settings.enabled_routers),
        "routers_skipped": len(container.registry.skipped),
        "collectors_active": len(container.collection.collectors),
        "backhauls_configured": len(container.collection.backhauls),
        "backhaul_provider": settings.backhaul_provider,
        "plan_provider": settings.plan_provider,
        "enforcement_enabled": container.shaping.enforcement_enabled,
        "enforcement_locked": container.shaping.enforcement_locked,
        "stale_jobs": stale,
        "failing_jobs": failing,
        "jobs": jobs,
        "uptime_s": (datetime.now(tz=UTC) - container.started_at).total_seconds(),
    }
