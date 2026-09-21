"""Sonde de vie et sonde de disponibilite.

Deux sondes, deux questions distinctes :

  /health        le processus repond-il ? (liveness) — jamais 503 tant qu'on
                 tourne, aucune dependance : c'est elle qui evite qu'un
                 orchestrateur tue un conteneur juste parce que la base tousse.
  /health/ready  la DONNEE arrive-t-elle ? (readiness) — 503 des qu'un
                 collecteur echoue durablement, pour retirer l'instance du
                 service ou alerter.

Le piege corrige ici : mesurer si la BOUCLE tourne n'est pas mesurer si la
donnee ARRIVE. Un job qui s'execute a l'heure mais echoue a chaque cycle n'est
jamais « en retard » ; l'ancien verdict ``db_ok and not stale`` le declarait donc
pret alors que plus aucune metrique n'entrait. La readiness regarde desormais la
fraicheur du dernier SUCCES de chaque job et son taux d'echec sur fenetre
glissante, pas seulement la cadence de la boucle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import ContainerDep

router = APIRouter(tags=["health"])

# Un job dont la boucle n'a pas tourne depuis plus de N periodes : la boucle
# elle-meme est bloquee (tache morte, event loop saturee).
STALE_RUN_FACTOR = 3
# Un job dont le dernier SUCCES remonte a plus de N periodes : la donnee ne
# rentre plus, meme si la boucle tourne encore.
STALE_DATA_FACTOR = 3
# Au-dela de ce taux d'echec sur la fenetre glissante, le job est considere en
# echec durable (et non victime d'un alea isole).
FAILURE_RATE_THRESHOLD = 0.5
# Nombre d'echecs d'affilee a partir duquel on ne parle plus d'alea.
CONSECUTIVE_FAILURE_LIMIT = 3
# On ne juge un taux d'echec qu'apres quelques executions : sinon un premier
# cycle un peu lent au demarrage ferait basculer la sonde a tort.
MIN_SAMPLES_FOR_RATE = 3


def _job_is_unhealthy(job: dict[str, Any]) -> tuple[bool, str | None]:
    """Un job est-il en echec DURABLE ? Renvoie (verdict, raison lisible).

    Trois signaux, chacun suffisant :
      - la boucle ne tourne plus (retard sur la cadence) ;
      - la donnee n'est plus fraiche (aucun succes depuis plusieurs periodes) ;
      - le taux d'echec sur la fenetre glissante est trop haut, ou les echecs
        s'enchainent.
    """
    interval = job["interval_s"] or 0
    since_run = job["seconds_since_last_run"]
    since_success = job["seconds_since_last_success"]
    runs = job["runs"]
    rate = job["recent_failure_rate"]
    window = job["recent_window"]

    # La boucle est-elle bloquee ? (cadence non tenue, tache morte)
    if since_run is not None and interval and since_run > interval * STALE_RUN_FACTOR:
        return True, "boucle en retard : cadence non tenue"

    # Le job a-t-il deja reussi au moins une fois ? S'il a tourne plusieurs fois
    # sans jamais aboutir, la donnee n'est jamais entree.
    if runs >= MIN_SAMPLES_FOR_RATE and since_success is None:
        return True, "aucun cycle reussi depuis le demarrage"

    # La donnee est-elle encore fraiche ? (dernier succes trop ancien)
    if since_success is not None and interval and since_success > interval * STALE_DATA_FACTOR:
        return True, "aucun succes recent : la donnee n'est plus fraiche"

    # Echec durable sur la fenetre glissante.
    if (
        rate is not None
        and window >= MIN_SAMPLES_FOR_RATE
        and rate >= FAILURE_RATE_THRESHOLD
        and job["last_ok"] is False
    ):
        return True, f"taux d'echec {rate:.0%} sur les {window} derniers cycles"

    if job["consecutive_failures"] >= CONSECUTIVE_FAILURE_LIMIT:
        return True, f"{job['consecutive_failures']} echecs consecutifs"

    return False, None


@router.get("/health", summary="Liveness (the process answers)")
async def health() -> dict[str, object]:
    """Liveness pure : ne touche ni la base ni les collecteurs.

    Tant que le processus repond, elle renvoie 200. Piloter une rotation de
    conteneur sur elle serait une erreur : c'est /health/ready qui juge l'etat
    reel du service.
    """
    return {
        "status": "ok",
        "version": __version__,
        "ts": datetime.now(tz=UTC).isoformat(),
    }


@router.get("/health/ready", summary="Readiness (database + data freshness)")
async def readiness(container: ContainerDep, response: Response) -> dict[str, object]:
    db_ok = await container.database.ping()
    settings = container.settings

    jobs = container.scheduler.status()

    # La boucle est en retard (tache morte, event loop saturee).
    stale: list[str] = []
    # La donnee n'arrive plus (echec durable ou dernier succes trop ancien).
    degraded_jobs: list[dict[str, str]] = []
    for job in jobs:
        since = job["seconds_since_last_run"]
        interval = job["interval_s"] or 0
        if since is not None and interval and since > interval * STALE_RUN_FACTOR:
            stale.append(job["job"])
        unhealthy, reason = _job_is_unhealthy(job)
        if unhealthy:
            degraded_jobs.append({"job": job["job"], "reason": reason or "en echec durable"})

    failing = [job["job"] for job in jobs if job["last_ok"] is False]

    # Le verdict depend desormais de la FRAICHEUR de la donnee, pas seulement de
    # la cadence de la boucle : un collecteur qui echoue durablement rend
    # l'instance non prete, meme si sa boucle tourne a l'heure.
    ready = db_ok and not degraded_jobs
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
        # Jobs qui font basculer la sonde en 503, avec la raison de chacun.
        "degraded_jobs": degraded_jobs,
        "failing_jobs": failing,
        "jobs": jobs,
        "uptime_s": (datetime.now(tz=UTC) - container.started_at).total_seconds(),
    }
