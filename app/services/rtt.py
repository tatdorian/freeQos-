"""Sonde de latence par abonne.

POURQUOI C'EST UNE SONDE ACTIVE
-------------------------------
LibreQoS mesure le RTT *passivement*, en lisant les horodatages TCP des paquets
qui le traversent. C'est possible parce qu'il est dans le chemin. Hors-bande, ce
signal n'existe pas : la seule mesure accessible est une sonde active depuis le
routeur, via ``/ping``.

Consequences assumees :
  - la mesure coute du CPU au routeur, donc la sonde est DESACTIVEE par defaut
    et limitee a un lot par cycle, en tourniquet ;
  - un abonne dont le pare-feu bloque l'ICMP ne repondra jamais : on ecrit None,
    pas une valeur inventee ;
  - c'est une latence a vide, pas une latence sous charge. Le vrai indicateur de
    QoE (phase 3) devra la correler au debit instantane de l'abonne.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.collectors.mikrotik import MikrotikCollector

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RttReading:
    rtt_ms: float | None
    measured_at: float


class RttProber:
    """Sonde un lot d'abonnes par cycle, en tourniquet.

    Les mesures sont conservees en memoire et rattachees a l'echantillon ecrit
    par le cycle de collecte : une seule ligne par abonne et par cycle, plutot
    que des lignes supplementaires ne portant qu'un RTT.
    """

    def __init__(
        self,
        *,
        batch_size: int = 20,
        max_age_s: float = 300.0,
        count: int = 1,
        clock=time.monotonic,
    ) -> None:
        self.batch_size = max(1, batch_size)
        self.max_age_s = max_age_s
        self.count = max(1, count)
        self._clock = clock
        self._readings: dict[int, RttReading] = {}
        self._cursor = 0
        self.probes_sent = 0
        self.probes_answered = 0

    def get(self, subscriber_id: int) -> float | None:
        """Derniere mesure, si elle n'est pas perimee."""
        reading = self._readings.get(subscriber_id)
        if reading is None:
            return None
        if self._clock() - reading.measured_at > self.max_age_s:
            return None
        return reading.rtt_ms

    def forget_all_but(self, subscriber_ids: set[int]) -> None:
        for subscriber_id in self._readings.keys() - subscriber_ids:
            del self._readings[subscriber_id]

    async def probe(self, targets: list[tuple[int, str, MikrotikCollector]]) -> int:
        """Sonde le prochain lot. ``targets`` = (subscriber_id, ip, collecteur).

        Retourne le nombre de mesures obtenues.
        """
        if not targets:
            return 0

        # Tourniquet : chaque abonne finit par etre sonde, sans jamais envoyer
        # une rafale de pings a tout le parc en meme temps.
        if self._cursor >= len(targets):
            self._cursor = 0
        batch = targets[self._cursor : self._cursor + self.batch_size]
        self._cursor += len(batch)

        results = await asyncio.gather(
            *(collector.ping(ip, self.count) for _, ip, collector in batch),
            return_exceptions=True,
        )

        now = self._clock()
        answered = 0
        for (subscriber_id, ip, collector), outcome in zip(batch, results, strict=True):
            if isinstance(outcome, BaseException):
                logger.debug("Ping %s via %s impossible : %s", ip, collector.name, outcome)
                rtt = None
            else:
                rtt = outcome
            self._readings[subscriber_id] = RttReading(rtt_ms=rtt, measured_at=now)
            self.probes_sent += 1
            if rtt is not None:
                answered += 1
        self.probes_answered += answered
        return answered

    def stats(self) -> dict[str, object]:
        return {
            "tracked": len(self._readings),
            "probes_sent": self.probes_sent,
            "probes_answered": self.probes_answered,
            "batch_size": self.batch_size,
        }
