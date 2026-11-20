"""La charge CPU des routeurs, relevee en continu : pic de l'heure et alerte.

POURQUOI. La page Router health lisait le CPU A L'INSTANT, et seulement la
moyenne des coeurs. Or ce qui fait monter la latence, c'est :

- le coeur le PLUS charge, pas la moyenne (un coeur sature sur quatre
  s'affiche 33 %) ;
- les PICS, qu'une lecture faite au calme ne voit pas.

Toutes les 30 s, chaque routeur est lu (``/system/resource`` et
``/system/resource/cpu``) et l'heure ecoulee est gardee en memoire. La page
montre le pic de l'heure ; la surveillance previent quand un coeur reste
au-dessus de 80 % (seuil au-dela duquel les files commencent a ajouter de la
latence).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

JOB_ROUTER_CPU = "router_cpu"
INTERVALLE_S = 30.0
FENETRE = timedelta(hours=1)
#: Un coeur au-dessus : les files commencent a ajouter de la latence.
SEUIL_ALERTE = 80
#: Au-dessus : critique.
SEUIL_CRITIQUE = 95
#: Releves consecutifs au-dessus du seuil avant d'alerter (2 x 30 s = 1 min) :
#: un pic d'une seconde n'est pas une alerte.
RELEVES_AVANT_ALERTE = 2


class RouterCpu:
    def __init__(self) -> None:
        self._releves: dict[str, deque[dict[str, Any]]] = {}

    def record(self, routeur: str, sante: dict[str, Any], now: datetime | None = None) -> None:
        if not sante.get("reachable"):
            return
        moyenne = sante.get("cpu_load_pct")
        coeur = sante.get("cpu_max_core_pct")
        if moyenne is None and coeur is None:
            return
        now = now or datetime.now(tz=UTC)
        file = self._releves.setdefault(routeur, deque())
        pic = coeur if coeur is not None else moyenne
        file.append(
            {"at": now, "avg": moyenne, "max_core": pic, "mem": sante.get("memory_used_pct")}
        )
        while file and now - file[0]["at"] > FENETRE:
            file.popleft()

    def summary(self, routeur: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(tz=UTC)
        file = [r for r in self._releves.get(routeur, ()) if now - r["at"] <= FENETRE]
        if not file:
            return {"samples": 0}
        pic = max(file, key=lambda r: r["max_core"] or 0)
        au_dessus = sum(1 for r in file if (r["max_core"] or 0) >= SEUIL_ALERTE)
        moyennes = [r["avg"] for r in file if r["avg"] is not None]
        return {
            "samples": len(file),
            "since": file[0]["at"].isoformat(),
            "peak_core_pct": pic["max_core"],
            "peak_at": pic["at"].isoformat(),
            "avg_pct": round(sum(moyennes) / len(moyennes), 1) if moyennes else None,
            "minutes_over_threshold": round(au_dessus * INTERVALLE_S / 60, 1),
            "threshold_pct": SEUIL_ALERTE,
            # L'heure ecoulee, releve par releve : la courbe de la page Router health.
            "history": [
                {
                    "at": r["at"].isoformat(),
                    "avg": r["avg"],
                    "max_core": r["max_core"],
                    "mem": r.get("mem"),
                }
                for r in file
            ],
        }

    def latest(self) -> dict[str, dict[str, Any]]:
        """Le dernier releve de chaque routeur : {avg, max_core, mem, at}."""
        return {routeur: dict(file[-1]) for routeur, file in self._releves.items() if file}

    def problems(self) -> list[dict[str, Any]]:
        """Pour la surveillance : un coeur au-dessus du seuil depuis une minute."""
        problemes = []
        for routeur, file in self._releves.items():
            derniers = list(file)[-RELEVES_AVANT_ALERTE:]
            if len(derniers) < RELEVES_AVANT_ALERTE:
                continue
            if all((r["max_core"] or 0) >= SEUIL_ALERTE for r in derniers):
                coeur = derniers[-1]["max_core"]
                problemes.append(
                    {
                        "key": f"cpu:{routeur}",
                        "category": "router",
                        "level": "crit" if coeur >= SEUIL_CRITIQUE else "warn",
                        "title": f"Router {routeur}: CPU core at {coeur} %",
                        "detail": f"Its busiest core has stayed above {SEUIL_ALERTE} % for over a "
                        "minute: queues may add latency for every client of this router. Look at "
                        "Router health, and lighten the load (rules, NetFlow export).",
                    }
                )
        return problemes

    async def sample(self, collectors: list[Any]) -> int:
        """Un releve de tous les routeurs, en parallele. Rend le nombre lu."""
        resultats = await asyncio.gather(*(c.health() for c in collectors), return_exceptions=True)
        lus = 0
        for collecteur, resultat in zip(collectors, resultats, strict=True):
            if isinstance(resultat, BaseException):
                logger.debug("CPU de %s illisible : %s", collecteur.name, resultat)
                continue
            self.record(collecteur.name, resultat)
            lus += 1
        return lus
