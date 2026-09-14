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

UN TOURNIQUET PAR POP, PAS UN POUR TOUT LE PARC
-----------------------------------------------
Le tourniquet a longtemps ete unique : un curseur, un lot par cycle, pour
l'ensemble des abonnes tous PoPs confondus. Le delai de re-sondage d'un abonne
donne croissait donc avec la taille TOTALE du parc -- un PoP de 3 abonnes
attendait derriere un PoP de 800.

C'est le mauvais decoupage, pour une raison physique : le ping est emis par LE
ROUTEUR de l'abonne. La ressource a menager, c'est le CPU de ce routeur-la, pas
une enveloppe globale qui n'existe nulle part. Chaque PoP a donc desormais son
propre curseur et son propre lot de ``batch_size`` : la charge par routeur est
exactement celle d'avant, et la fraicheur d'un abonne ne depend plus que du
nombre d'abonnes de SON PoP.

Consequence a connaitre : le nombre total de pings d'un cycle devient
``batch_size x nombre de PoPs sondes``. C'est voulu -- ces pings partent de
routeurs differents, ils ne se disputent rien.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.collectors.mikrotik import MikrotikCollector

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RttReading:
    rtt_ms: float | None
    measured_at: float


class RttProber:
    """Sonde un lot d'abonnes PAR POP et par cycle, en tourniquet.

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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Lot PAR POP : c'est le routeur qui emet les pings, donc c'est par
        # routeur que la charge se mesure.
        self.batch_size = max(1, batch_size)
        self.max_age_s = max_age_s
        self.count = max(1, count)
        self._clock = clock
        self._readings: dict[int, RttReading] = {}
        # Un curseur de tourniquet par PoP, jamais un seul pour tout le parc.
        self._cursors: dict[str, int] = {}
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
        """Sonde le prochain lot de CHAQUE PoP. ``targets`` = (id, ip, collecteur).

        Retourne le nombre de mesures obtenues.
        """
        if not targets:
            return 0

        par_pop = _group_by_pop(targets)
        # Un PoP retire de l'inventaire ne doit pas garder son curseur : il
        # fausserait le tourniquet le jour ou le meme nom reapparait.
        for nom in self._cursors.keys() - par_pop.keys():
            del self._cursors[nom]

        batch: list[tuple[int, str, MikrotikCollector]] = []
        for nom, cibles in par_pop.items():
            batch.extend(self._next_batch(nom, cibles))
        if not batch:
            return 0

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

    def _next_batch(
        self, pop: str, targets: list[tuple[int, str, MikrotikCollector]]
    ) -> list[tuple[int, str, MikrotikCollector]]:
        """Tourniquet d'UN PoP : chaque abonne de ce PoP finit par etre sonde,
        sans jamais envoyer une rafale de pings a tout le PoP en meme temps."""
        curseur = self._cursors.get(pop, 0)
        if curseur >= len(targets):
            curseur = 0
        lot = targets[curseur : curseur + self.batch_size]
        self._cursors[pop] = curseur + len(lot)
        return lot

    def stats(self) -> dict[str, object]:
        return {
            "tracked": len(self._readings),
            "probes_sent": self.probes_sent,
            "probes_answered": self.probes_answered,
            # Lot par POP, pas pour le parc entier : le nombre de PoPs sondes dit
            # combien de tourniquets tournent en parallele.
            "batch_size": self.batch_size,
            "pops": len(self._cursors),
        }


def _group_by_pop(
    targets: list[tuple[int, str, MikrotikCollector]],
) -> dict[str, list[tuple[int, str, MikrotikCollector]]]:
    """Range les cibles par routeur emetteur, en preservant leur ordre.

    La cle est le nom du COLLECTEUR : c'est lui qui ouvre la session API et qui
    emet le ping, donc c'est lui dont on menage le CPU. Deux routeurs declares
    sous un meme ``pop_name`` gardent ainsi chacun leur tourniquet.
    """
    par_pop: dict[str, list[tuple[int, str, MikrotikCollector]]] = {}
    for cible in targets:
        par_pop.setdefault(cible[2].name, []).append(cible)
    return par_pop
