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

from app.collectors.mikrotik import MikrotikCollector, upstream_of
from app.models import PingStats

logger = logging.getLogger(__name__)


async def ping_per_router(
    targets: list[tuple[str, MikrotikCollector]], count: int, interval_ms: int
) -> list[PingStats | BaseException]:
    """Les series de pings, UNE A LA FOIS par routeur, en parallele entre routeurs.

    Une connexion RouterOS ne traite qu'une commande a la fois. Les lancer
    toutes ensemble ne les rendait pas plus rapides : chacune bloquait un
    thread en attendant son tour, epuisait le reservoir de threads partage par
    toute l'application (collecte et pages comprises), et les dernieres
    depassaient leur delai avant meme d'avoir commence -- comptees "sans
    reponse" alors qu'elles n'etaient jamais parties.
    """
    resultats: list[PingStats | BaseException | None] = [None] * len(targets)
    par_routeur: dict[int, list[int]] = {}
    for i, (_cible, collector) in enumerate(targets):
        par_routeur.setdefault(id(collector), []).append(i)

    async def file(indices: list[int]) -> None:
        for i in indices:
            cible, collector = targets[i]
            try:
                resultats[i] = await collector.ping_stats(cible, count, interval_ms=interval_ms)
            except Exception as exc:  # noqa: BLE001 - une cible muette n'arrete pas les autres
                resultats[i] = exc

    await asyncio.gather(*(file(indices) for indices in par_routeur.values()))
    return [r if r is not None else RuntimeError("not probed") for r in resultats]


@dataclass(slots=True)
class RttReading:
    rtt_ms: float | None
    measured_at: float
    #: La serie complete : mediane, extremes, gigue, perte. None quand le
    #: routeur n'a pas pu lancer la sonde (erreur, delai depasse).
    stats: PingStats | None = None
    router_name: str | None = None


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
        count: int = 5,
        interval_ms: int = 200,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Lot PAR POP : c'est le routeur qui emet les pings, donc c'est par
        # routeur que la charge se mesure.
        self.batch_size = max(1, batch_size)
        self.max_age_s = max_age_s
        self.count = max(1, count)
        self.interval_ms = max(10, interval_ms)
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

    def detail(self, subscriber_id: int) -> dict[str, object] | None:
        """La serie derriere le chiffre : mediane, extremes, gigue, perte, age."""
        reading = self._readings.get(subscriber_id)
        if reading is None:
            return None
        age = self._clock() - reading.measured_at
        if age > self.max_age_s:
            return None
        base: dict[str, object] = {
            "age_s": round(age, 1),
            "router": reading.router_name,
            "method": f"{self.count} x ping, {self.interval_ms} ms apart, from the PoP router",
        }
        if reading.stats is not None:
            base.update(reading.stats.to_dict())
        return base

    def readings_by_router(self) -> dict[str, list[PingStats]]:
        """Series fraiches, rangees par routeur emetteur (latence d'acces du PoP)."""
        maintenant = self._clock()
        par_routeur: dict[str, list[PingStats]] = {}
        for reading in self._readings.values():
            if reading.stats is None or reading.router_name is None:
                continue
            if maintenant - reading.measured_at > self.max_age_s:
                continue
            par_routeur.setdefault(reading.router_name, []).append(reading.stats)
        return par_routeur

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

        results = await ping_per_router(
            [(ip, collector) for _, ip, collector in batch], self.count, self.interval_ms
        )

        now = self._clock()
        answered = 0
        for (subscriber_id, ip, collector), outcome in zip(batch, results, strict=True):
            stats: PingStats | None
            if isinstance(outcome, BaseException):
                logger.debug("Ping %s via %s impossible : %s", ip, collector.name, outcome)
                stats = None
            else:
                stats = outcome
            # La MEDIANE, plus le minimum : le meilleur paquet d'une serie cache
            # exactement ce qu'on cherche, l'attente dans une file qui se remplit.
            rtt = stats.median_ms if stats is not None else None
            self._readings[subscriber_id] = RttReading(
                rtt_ms=rtt, measured_at=now, stats=stats, router_name=collector.name
            )
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
            "count": self.count,
            "interval_ms": self.interval_ms,
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


# =========================================================================
# Latence par SEGMENT : ou se trouve le retard
# =========================================================================


@dataclass(slots=True)
class PathReading:
    target: str
    stats: PingStats | None
    measured_at: float
    error: str | None = None


class PathProber:
    """Sonde, depuis CHAQUE routeur, sa passerelle amont et des cibles internet.

    POURQUOI. La latence d'un abonne ne dit pas OU se perd le temps. Mesuree du
    PoP vers l'abonne, elle couvre l'acces (radio, VLAN) ; pour savoir si le
    coeur ou le transit internet ralentissent, il faut aussi mesurer depuis ce
    PoP vers le haut. Trois segments, trois mesures depuis le meme routeur :

      - acces    : PoP -> abonnes (le tourniquet des abonnes, deja la) ;
      - amont    : PoP -> sa passerelle par defaut (le lien vers le coeur) ;
      - internet : PoP -> des cibles publiques stables (anycast).

    Si l'amont est bon et internet mauvais, le probleme est au-dessus du coeur.
    Si l'amont est deja mauvais, il est entre le PoP et le coeur. C'est ce
    diagnostic que la seule latence abonne ne peut pas poser.

    La passerelle vient de la TABLE DE ROUTAGE lue a la decouverte (route par
    defaut active), jamais d'une saisie.
    """

    def __init__(
        self,
        *,
        internet_targets: tuple[str, ...] = ("1.1.1.1", "8.8.8.8"),
        count: int = 5,
        interval_ms: int = 200,
        max_age_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.internet_targets = tuple(t for t in internet_targets if t)
        self.count = max(1, count)
        self.interval_ms = max(10, interval_ms)
        self.max_age_s = max_age_s
        self._clock = clock
        #: routeur -> segment ("gateway" | "internet") -> lectures
        self._readings: dict[str, dict[str, list[PathReading]]] = {}

    async def probe(self, collectors: list[MikrotikCollector]) -> int:
        taches: list[tuple[str, str, str, MikrotikCollector]] = []
        for collector in collectors:
            passerelle, _interface = upstream_of(collector.name)
            if passerelle:
                taches.append((collector.name, "gateway", passerelle, collector))
            for cible in self.internet_targets:
                taches.append((collector.name, "internet", cible, collector))
        if not taches:
            return 0
        resultats = await ping_per_router(
            [(cible, collector) for _, _, cible, collector in taches], self.count, self.interval_ms
        )
        maintenant = self._clock()
        nouvelles: dict[str, dict[str, list[PathReading]]] = {}
        repondu = 0
        for (routeur, segment, cible, _c), resultat in zip(taches, resultats, strict=True):
            if isinstance(resultat, BaseException):
                lecture = PathReading(cible, None, maintenant, error=str(resultat) or "error")
            else:
                lecture = PathReading(cible, resultat, maintenant)
                if resultat.received:
                    repondu += 1
            nouvelles.setdefault(routeur, {}).setdefault(segment, []).append(lecture)
        self._readings = nouvelles
        return repondu

    def snapshot(self) -> dict[str, dict[str, list[dict[str, object]]]]:
        maintenant = self._clock()
        sortie: dict[str, dict[str, list[dict[str, object]]]] = {}
        for routeur, segments in self._readings.items():
            for segment, lectures in segments.items():
                for lecture in lectures:
                    age = maintenant - lecture.measured_at
                    if age > self.max_age_s:
                        continue
                    ligne: dict[str, object] = {
                        "target": lecture.target,
                        "age_s": round(age, 1),
                        "error": lecture.error,
                    }
                    if lecture.stats is not None:
                        ligne.update(lecture.stats.to_dict())
                    sortie.setdefault(routeur, {}).setdefault(segment, []).append(ligne)
        return sortie


def summarise(series: list[PingStats]) -> dict[str, object] | None:
    """Latence d'ACCES d'un PoP : ce que vivent ses abonnes, en un chiffre juste.

    La mediane des medianes (l'abonne typique), le 90e centile (les plus mal
    servis, qu'une moyenne noierait) et la perte moyenne.
    """
    medianes = sorted(s.median_ms for s in series if s.median_ms is not None)
    if not medianes:
        return None

    def rang(q: float) -> float:
        return medianes[min(len(medianes) - 1, int(round(q * (len(medianes) - 1))))]

    pertes = [s.loss_pct for s in series if s.loss_pct is not None]
    gigues = [s.jitter_ms for s in series if s.jitter_ms is not None]
    return {
        "median_ms": round(rang(0.5), 2),
        "p90_ms": round(rang(0.9), 2),
        "max_ms": round(medianes[-1], 2),
        "jitter_ms": round(sum(gigues) / len(gigues), 2) if gigues else None,
        "loss_pct": round(sum(pertes) / len(pertes), 1) if pertes else None,
        "subscribers": len(medianes),
    }
