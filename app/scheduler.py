"""Boucle de collecte periodique.

Un job = une tache asyncio qui enchaine ``executer -> dormir le reste de la
periode``. Consequence voulue : deux executions du meme job ne peuvent jamais se
chevaucher. Si un cycle deborde sa periode (routeur lent, base saturee), on le
signale et on repart immediatement plutot que d'empiler des executions
concurrentes sur les memes connexions API.

Cette boucle est la boucle CENTRALE LENTE. Elle n'a pas vocation a descendre a la
milliseconde : la reaction rapide aux fades radio appartient a la boucle locale
du PoP, hors perimetre de cette application.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.models import RunResult

logger = logging.getLogger(__name__)

JobFn = Callable[[], Awaitable[RunResult | None]]

# Fenetre glissante des derniers verdicts d'un job. Assez large pour lisser un
# echec isole (un routeur momentanement muet), assez courte pour qu'un collecteur
# qui repart soit reconnu sain rapidement.
RECENT_WINDOW = 20


@dataclass
class JobState:
    name: str
    interval_s: float
    fn: JobFn
    runs: int = 0
    failures: int = 0
    overruns: int = 0
    last_started_at: float | None = None
    last_duration_s: float | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    # Fraicheur de la DONNEE, pas seulement de la boucle : quand un cycle a-t-il
    # reellement REUSSI pour la derniere fois ? Un job qui tourne a l'heure mais
    # echoue a chaque fois n'a pas de dernier succes recent, et la readiness doit
    # le voir. On garde l'horloge monotone (pour le calcul de fraicheur) et un
    # horodatage mural (pour l'affichage).
    last_success_monotonic: float | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    recent: deque[bool] = field(default_factory=lambda: deque(maxlen=RECENT_WINDOW))
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def record(self, ok: bool) -> None:
        """Enregistre le verdict d'un cycle, pour la fenetre glissante et la
        fraicheur de la donnee."""
        self.recent.append(ok)
        if ok:
            self.last_success_monotonic = time.monotonic()
            self.last_success_at = datetime.now(tz=UTC)
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

    def snapshot(self) -> dict[str, Any]:
        recent = list(self.recent)
        failure_rate = (recent.count(False) / len(recent)) if recent else None
        return {
            "job": self.name,
            "interval_s": self.interval_s,
            "runs": self.runs,
            "failures": self.failures,
            "overruns": self.overruns,
            "last_duration_s": self.last_duration_s,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "seconds_since_last_run": (
                None if self.last_started_at is None else time.monotonic() - self.last_started_at
            ),
            # Fraicheur de la donnee : depuis combien de temps aucun cycle n'a
            # reussi. C'est ce que la sonde de readiness doit regarder, pas
            # seulement si la boucle tourne.
            "last_success_at": self.last_success_at,
            "seconds_since_last_success": (
                None
                if self.last_success_monotonic is None
                else time.monotonic() - self.last_success_monotonic
            ),
            "consecutive_failures": self.consecutive_failures,
            "recent_failure_rate": failure_rate,
            "recent_window": len(recent),
        }


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._stop = asyncio.Event()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def add_job(self, name: str, interval_s: float, fn: JobFn) -> None:
        if interval_s <= 0:
            logger.info("Job '%s' desactive (intervalle <= 0)", name)
            return
        self._jobs[name] = JobState(name=name, interval_s=interval_s, fn=fn)

    async def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        for job in self._jobs.values():
            job.task = asyncio.create_task(self._loop(job), name=f"job:{job.name}")
        logger.info(
            "Scheduler demarre : %s",
            ", ".join(f"{j.name}@{j.interval_s:g}s" for j in self._jobs.values()) or "aucun job",
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for job in self._jobs.values():
            job.task = None
        self._running = False
        logger.info("Scheduler arrete")

    def status(self) -> list[dict[str, Any]]:
        return [job.snapshot() for job in self._jobs.values()]

    def job_names(self) -> list[str]:
        return list(self._jobs)

    async def run_once(self, name: str) -> RunResult | None:
        """Declenche un job hors cadence (utile pour l'API d'admin et les tests)."""
        job = self._jobs.get(name)
        if job is None:
            raise KeyError(name)
        return await self._execute(job)

    # ------------------------------------------------------------------
    async def _loop(self, job: JobState) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            await self._execute(job)
            elapsed = time.monotonic() - started

            if elapsed > job.interval_s:
                job.overruns += 1
                logger.warning(
                    "Job '%s' a depasse sa periode (%.2fs > %.2fs) : cadence non tenue",
                    job.name,
                    elapsed,
                    job.interval_s,
                )
                delay = 0.0
            else:
                delay = job.interval_s - elapsed

            # Attendre sur l'evenement d'arret rend le shutdown immediat.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _execute(self, job: JobState) -> RunResult | None:
        job.last_started_at = time.monotonic()
        job.runs += 1
        try:
            result = await job.fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - un job ne doit jamais tuer sa boucle
            job.failures += 1
            job.last_ok = False
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.last_duration_s = time.monotonic() - job.last_started_at
            job.record(ok=False)
            logger.exception("Job '%s' en echec", job.name)
            return None

        job.last_duration_s = time.monotonic() - job.last_started_at
        if result is not None:
            job.last_ok = result.ok
            job.last_error = result.error_text
            if not result.ok:
                job.failures += 1
            job.record(ok=result.ok)
        else:
            job.last_ok = True
            job.last_error = None
            job.record(ok=True)
        return result
