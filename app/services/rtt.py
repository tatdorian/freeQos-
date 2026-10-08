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
    #: Pourquoi le routeur n'a pas pu pinguer (src-address refusee, delai...).
    #: Sans elle, l'interface affichait "-" comme si rien n'avait ete tente.
    error: str | None = None
    #: L'adresse sondee : c'est elle que le diagnostic rejoue.
    address: str | None = None


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
        self._erreurs_vues: dict[str, str] = {}
        # Abonnes qui ont DEJA repondu au moins une fois. Un client qui n'a
        # jamais repondu n'est pas « en panne » : sa box (CPE) ou son pare-feu
        # bloque le ping, ce qui est courant. Son silence n'est ni une
        # degradation QoE, ni un motif d'action.
        self._ont_repondu: set[int] = set()

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
        if reading.error:
            base["error"] = reading.error
        base["ever_answered"] = subscriber_id in self._ont_repondu
        return base

    def silent_target(self) -> tuple[str, str] | None:
        """(routeur, adresse) du client muet le plus recemment sonde.

        C'est ce que le bouton « Diagnose » rejoue quand la sonde est muette :
        l'exploitant n'a pas a chercher une IP et un nom de routeur.
        """
        muets = [
            r
            for r in self._readings.values()
            if r.stats is not None
            and r.stats.sent
            and not r.stats.received
            and r.router_name
            and r.address
        ]
        if not muets:
            return None
        dernier = max(muets, key=lambda r: r.measured_at)
        return str(dernier.router_name), str(dernier.address)

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
        """Oublie les abonnes partis -- une fois leur mesure PERIMEE seulement.

        Les oublier des le premier cycle ou ils manquent effacait leur latence
        a chaque raté de collecte (routeur qui repond mal une fois, session qui
        se reconnecte) : l'interface repassait a "-" jusqu'a la sonde suivante.
        La derniere mesure reste donc tant qu'elle est fraiche (``max_age_s``) ;
        l'etat reste borne, puisque tout finit par perimer.
        """
        maintenant = self._clock()
        for subscriber_id in self._readings.keys() - subscriber_ids:
            if maintenant - self._readings[subscriber_id].measured_at > self.max_age_s:
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
            erreur: str | None = None
            if isinstance(outcome, BaseException):
                erreur = str(outcome) or type(outcome).__name__
                # Une fois par routeur et par message : visible dans les
                # journaux sans les noyer a chaque cycle.
                if self._erreurs_vues.get(collector.name) != erreur:
                    self._erreurs_vues[collector.name] = erreur
                    logger.warning("Ping %s via %s impossible : %s", ip, collector.name, erreur)
                stats = None
            else:
                stats = outcome
                self._erreurs_vues.pop(collector.name, None)
            # La MEDIANE, plus le minimum : le meilleur paquet d'une serie cache
            # exactement ce qu'on cherche, l'attente dans une file qui se remplit.
            rtt = stats.median_ms if stats is not None else None
            self._readings[subscriber_id] = RttReading(
                rtt_ms=rtt,
                measured_at=now,
                stats=stats,
                router_name=collector.name,
                error=erreur,
                address=ip,
            )
            self.probes_sent += 1
            if rtt is not None:
                answered += 1
                self._ont_repondu.add(subscriber_id)
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


# ------------------------------------------------------------ diagnostic
def _vrai(valeur: object) -> bool:
    return str(valeur).lower() in ("true", "yes")


def suspicious_firewall_rules(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Regles du pare-feu qui peuvent jeter les pings de la sonde.

    La sonde part du routeur (chaine ``output``) et sa reponse y revient
    (chaine ``input``). Une regle ``drop``/``reject`` de ces chaines, ICMP ou
    tous protocoles, placee AVANT le ``accept`` des connexions etablies, jette
    la reponse : c'est le durcissement classique « on bloque tout ce qui vient
    des clients » qui rend la sonde muette chez TOUS les clients a la fois.
    """
    suspectes: list[dict[str, object]] = []
    etablies_acceptees: set[str] = set()
    for position, regle in enumerate(rows):
        chaine = str(regle.get("chain") or "")
        if chaine not in ("input", "output") or _vrai(regle.get("disabled")):
            continue
        action = str(regle.get("action") or "")
        etat = str(regle.get("connection-state") or "")
        protocole = str(regle.get("protocol") or "")
        # Un accept des connexions etablies, ou de l'ICMP, laisse passer la reponse.
        if action == "accept" and (
            ("established" in etat and protocole in ("", "icmp")) or protocole == "icmp"
        ):
            etablies_acceptees.add(chaine)
            continue
        if action not in ("drop", "reject") or chaine in etablies_acceptees:
            continue
        if protocole not in ("", "icmp"):
            continue
        suspectes.append(
            {
                "position": position,
                "chain": chaine,
                "action": action,
                "protocol": protocole or "any",
                "in_interface": regle.get("in-interface") or regle.get("in-interface-list"),
                "src_address": regle.get("src-address") or regle.get("src-address-list"),
                "comment": regle.get("comment"),
            }
        )
    return suspectes


def rtt_verdict(
    attempts: list[dict[str, object]],
    control: dict[str, object] | None,
    suspects: list[dict[str, object]],
) -> dict[str, str]:
    """Une cause, en une phrase, a partir des essais du diagnostic.

    ``attempts`` : les pings vers le client (avec puis sans source) ;
    ``control``  : le meme ping vers la passerelle du routeur -- s'il repond,
    le routeur SAIT pinguer et le silence vient du cote client.
    """
    erreurs = " ".join(str(a.get("error") or "") for a in attempts).lower()
    if "permission" in erreurs or "not allowed" in erreurs:
        return {
            "code": "no_test_policy",
            "message": "The router account used by freeQoS may not run /ping: give its group "
            "the 'test' policy (System > Users > Groups).",
        }

    def recus(essai: dict[str, object] | None) -> int:
        stats = (essai or {}).get("stats")
        return int(stats.get("received") or 0) if isinstance(stats, dict) else 0

    rapides = [a for a in attempts if a.get("interval") != "1s (terminal)"]
    lents = [a for a in attempts if a.get("interval") == "1s (terminal)"]
    if lents and any(recus(a) for a in lents) and not any(recus(a) for a in rapides):
        return {
            "code": "rate_limited",
            "message": "The client answers one ping per second (like the router's terminal) but "
            "drops a quick burst: its ICMP rate limit, or a device in between, throws the probe "
            "away. The probe switches to one packet per second on this router by itself.",
        }
    if any(recus(a) for a in attempts):
        sans_source = [a for a in attempts if not a.get("source") and recus(a)]
        avec_source = [a for a in attempts if a.get("source") and recus(a)]
        if sans_source and not avec_source and any(a.get("source") for a in attempts):
            return {
                "code": "loopback_return_path",
                "message": "The client answers, but not to the router's loopback: no return "
                "route to that address. The probe now pings without source on this router.",
            }
        return {
            "code": "ok",
            "message": "The client answers now. If it was silent before, it was momentary "
            "(client offline, line saturated).",
        }
    lignes: list[object] = []
    for a in attempts:
        brut = a.get("raw")
        if isinstance(brut, list):
            lignes.extend(brut)
    statuts = " ".join(
        str(ligne.get("status") or "") for ligne in lignes if isinstance(ligne, dict)
    ).lower()
    if "unreachable" in statuts or "no route" in statuts:
        return {
            "code": "no_route",
            "message": "The router has no route to this client (host unreachable): the address "
            "is not behind this router.",
        }
    if suspects:
        return {
            "code": "firewall",
            "message": f"{len(suspects)} firewall rule(s) on this router drop ICMP in the "
            "input/output chain before established connections are accepted: the client's "
            "replies are thrown away. Move an 'accept icmp' (or 'accept established,related') "
            "rule above them.",
        }
    if control is not None and recus(control):
        return {
            "code": "client_blocks_icmp",
            "message": "The router pings its gateway fine, but the client never answers: the "
            "client's box or firewall blocks ping (common on PPPoE CPEs and business "
            "firewalls), or a rule elsewhere drops ICMP toward clients. Allow ICMP echo on the "
            "CPE's WAN side to get latency.",
        }
    if control is not None:
        return {
            "code": "router_cannot_ping",
            "message": "The router gets no reply from its own gateway either: pings from this "
            "router are blocked (firewall output/input, or /ip/settings). Check the router "
            "itself first.",
        }
    return {
        "code": "client_blocks_icmp",
        "message": "No reply from the client, with or without source address: the client's box "
        "or firewall most likely blocks ping.",
    }
