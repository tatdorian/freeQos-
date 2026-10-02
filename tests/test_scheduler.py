"""Boucle de collecte periodique."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.models import RunResult
from app.scheduler import Scheduler


def _result(job: str = "test", ok: bool = True) -> RunResult:
    return RunResult(job=job, started_at=datetime.now(tz=UTC), duration_s=0.0, ok=ok, items=1)


async def test_le_job_s_execute_periodiquement() -> None:
    appels = 0

    async def job() -> RunResult:
        nonlocal appels
        appels += 1
        return _result()

    scheduler = Scheduler()
    scheduler.add_job("test", 0.02, job)
    await scheduler.start()
    await asyncio.sleep(0.12)
    await scheduler.stop()

    assert appels >= 3
    assert scheduler.running is False


async def test_les_executions_ne_se_chevauchent_jamais() -> None:
    """Propriete structurante : deux cycles ne doivent pas taper simultanement
    sur les memes sessions API routeur."""
    en_cours = 0
    chevauchements = 0

    async def job_lent() -> RunResult:
        nonlocal en_cours, chevauchements
        en_cours += 1
        if en_cours > 1:
            chevauchements += 1
        await asyncio.sleep(0.05)
        en_cours -= 1
        return _result()

    scheduler = Scheduler()
    scheduler.add_job("lent", 0.01, job_lent)  # periode plus courte que le job
    await scheduler.start()
    await asyncio.sleep(0.2)
    await scheduler.stop()

    assert chevauchements == 0
    assert scheduler.status()[0]["overruns"] > 0


async def test_une_exception_ne_tue_pas_la_boucle() -> None:
    appels = 0

    async def job_instable() -> RunResult:
        nonlocal appels
        appels += 1
        if appels == 1:
            raise RuntimeError("routeur injoignable")
        return _result()

    scheduler = Scheduler()
    scheduler.add_job("instable", 0.02, job_instable)
    await scheduler.start()
    await asyncio.sleep(0.1)
    await scheduler.stop()

    assert appels >= 2
    status = scheduler.status()[0]
    assert status["failures"] >= 1
    assert status["last_ok"] is True  # remis a l'endroit apres le passage suivant


async def test_resultat_en_echec_compte_comme_echec() -> None:
    async def job() -> RunResult:
        return _result(ok=False)

    scheduler = Scheduler()
    scheduler.add_job("ko", 60, job)
    result = await scheduler.run_once("ko")

    assert result is not None and result.ok is False
    assert scheduler.status()[0]["failures"] == 1


async def test_intervalle_nul_desactive_le_job() -> None:
    async def job() -> RunResult:
        return _result()

    scheduler = Scheduler()
    scheduler.add_job("desactive", 0, job)
    assert scheduler.job_names() == []


async def test_arret_immediat_meme_avec_une_longue_periode() -> None:
    """Le shutdown ne doit pas attendre la fin de la periode."""

    async def job() -> RunResult:
        return _result()

    scheduler = Scheduler()
    scheduler.add_job("lent", 3600, job)
    await scheduler.start()
    await asyncio.sleep(0.01)

    async with asyncio.timeout(1.0):
        await scheduler.stop()
    assert scheduler.running is False
