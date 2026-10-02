"""Score de QoE composite : ce que l'abonne RESSENT, pas ce que le lien mesure.

POURQUOI CE MODULE EXISTE
-------------------------
La heatmap Executif affichait jusqu'ici une QoE derivee du SEUL RTT, par une
fonction affine dont le docstring reconnaissait lui-meme le caractere
provisoire : « faute de latence sous charge en continu, on approxime ». Or la
latence sous charge, le projet sait desormais la mesurer
(``app.services.bufferbloat``) : correler RTT et debit du meme echantillon donne
une note A+..F qui, elle, dit vraiment ce que vit l'abonne.

Ce module fait le pont. Il produit UN score 0..100 a partir de deux composantes :

  * la latence A VIDE (le plancher du chemin : distance, encapsulation, radio) ;
  * le BUFFERBLOAT, c'est-a-dire ce que la charge AJOUTE a cette latence.

C'est le maillon FAIBLE qui fait la note : ``min`` des deux composantes. Une
latence de base deja mauvaise ne se rachete pas par l'absence de bufferbloat
(un lien satellite a 600 ms est injouable meme parfaitement gere), et
inversement un lien a 8 ms au repos qui monte a 400 ms sous charge n'est pas
« excellent ».

UN SEUL SCORE, DEUX LECTEURS
----------------------------
La heatmap Executif (``app.services.heatmap``) et la boucle fermee
(``ShapingService.adjust_for_qoe``) appellent ``compute_qoe`` — la meme
fonction, avec les memes seuils. C'etait la condition pour que la boucle fermee
reagisse a ce que l'operateur voit a l'ecran, et non a un proxy que le projet
documentait comme provisoire.

COHERENCE DES COULEURS
----------------------
La severite (ok / warn / crit) d'une note issue du bufferbloat est celle de la
NOTE A+..F correspondante, prise telle quelle dans ``GRADE_SEVERITY``. Elle
n'est pas rededuite du score : deux baremes independants finiraient par se
contredire (une pastille « B » verte a cote d'un score rouge). Les points
d'ancrage ci-dessous sont choisis pour que le NOMBRE tombe malgre tout dans la
bande de couleur correspondante, donc le chiffre et la couleur racontent la meme
chose.

Fonction PURE : ni base, ni reseau. Elle prend des millisecondes, elle rend une
note.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.bufferbloat import GRADE_SEVERITY, BufferbloatVerdict, grade_for_bloat

# --- Composante « latence a vide » -------------------------------------------
# En dessous de ce plancher la latence est imperceptible : inutile de penaliser
# un abonne parce que son PoP est a 9 ms plutot qu'a 3 ms.
IDLE_FREE_MS = 10.0
IDLE_PENALTY_PER_MS = 0.6

# --- Composante « bufferbloat » ----------------------------------------------
# Points d'ancrage (latence ajoutee en ms -> score), interpoles lineairement.
# Ils sont cales sur les bornes de GRADE_THRESHOLDS pour que chaque note tombe
# dans la bande de couleur de sa severite :
#   A+ [0, 5]     -> 90..100  (ok)
#   A  (5, 30]    -> 80..90   (ok)
#   B  (30, 60]   -> 65..80   (warn)
#   C  (60, 100]  -> 50..65   (warn)
#   D  (100, 200] -> 25..50   (crit)
#   F  (200, ...) -> 0..25    (crit)
BLOAT_ANCHORS: list[tuple[float, float]] = [
    (0.0, 100.0),
    (5.0, 90.0),
    (30.0, 80.0),
    (60.0, 65.0),
    (100.0, 50.0),
    (200.0, 25.0),
    (400.0, 0.0),
]

# D'ou vient la note, dit explicitement pour qu'un score de repli ne se fasse
# jamais passer pour une mesure sous charge.
BASIS_COMPOSITE = "composite"  # latence a vide ET bufferbloat
BASIS_LOAD = "load"  # bufferbloat seul (pas de RTT de reference)
BASIS_LATENCY = "latency"  # RTT seul : PROXY, aucune charge a correler

_SEVERITE_RANG = {"ok": 0, "warn": 1, "crit": 2}


def qoe_severity(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= 80:
        return "ok"
    if score >= 50:
        return "warn"
    return "crit"


def qoe_from_rtt(ms: float | None) -> float | None:
    """Score 0..100 derive de la SEULE latence a vide.

    Conserve comme REPLI, et seulement comme repli : tant qu'aucune charge n'est
    correlable (abonne silencieux, sonde RTT coupee), c'est tout ce qu'on a. Un
    score obtenu par ce chemin est marque ``basis="latency"`` pour qu'on ne le
    confonde jamais avec une mesure sous charge.
    """
    if ms is None:
        return None
    score = 100.0 - max(0.0, ms - IDLE_FREE_MS) * IDLE_PENALTY_PER_MS
    return round(max(0.0, min(100.0, score)), 0)


def qoe_from_bloat(bloat_ms: float | None) -> float | None:
    """Score 0..100 derive de la latence AJOUTEE sous charge."""
    if bloat_ms is None:
        return None
    valeur = max(0.0, float(bloat_ms))
    premier_ms, premier_score = BLOAT_ANCHORS[0]
    if valeur <= premier_ms:
        return premier_score
    for (bas_ms, bas_score), (haut_ms, haut_score) in zip(
        BLOAT_ANCHORS, BLOAT_ANCHORS[1:], strict=False
    ):
        if valeur <= haut_ms:
            fraction = (valeur - bas_ms) / (haut_ms - bas_ms)
            score = bas_score + (haut_score - bas_score) * fraction
            return round(max(0.0, min(100.0, score)), 0)
    # Au-dela du dernier point d'ancrage, le temps reel est deja impossible.
    return 0.0


def _pire(a: str, b: str) -> str:
    return a if _SEVERITE_RANG.get(a, 0) >= _SEVERITE_RANG.get(b, 0) else b


@dataclass(slots=True)
class QoeScore:
    score: float
    severity: str
    # "composite" | "load" | "latency" -- voir les constantes BASIS_*.
    basis: str
    rtt_ms: float | None
    bloat_ms: float | None
    # Note A+..F du bufferbloat, quand il y avait une charge a correler.
    grade: str | None

    @property
    def from_load(self) -> bool:
        """Vrai quand la note repose sur une latence SOUS CHARGE reellement
        mesuree, et non sur le proxy latence."""
        return self.grade is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "severity": self.severity,
            "basis": self.basis,
            "grade": self.grade,
            "rtt_ms": round(self.rtt_ms, 1) if self.rtt_ms is not None else None,
            "bloat_ms": round(self.bloat_ms, 1) if self.bloat_ms is not None else None,
        }


def compute_qoe(*, rtt_ms: float | None = None, bloat_ms: float | None = None) -> QoeScore | None:
    """Score de QoE composite, ou ``None`` quand on ne peut RIEN conclure.

    ``None`` est volontaire et doit le rester : sans RTT ni charge correlable, il
    n'y a pas de mesure, et afficher « 100 » sur un abonne jamais sonde serait un
    faux positif rassurant — exactement ce que ``compute_bufferbloat`` refuse
    deja de faire.
    """
    latence = qoe_from_rtt(rtt_ms)
    charge = qoe_from_bloat(bloat_ms)

    if charge is None:
        if latence is None:
            return None
        return QoeScore(
            score=latence,
            severity=qoe_severity(latence),
            basis=BASIS_LATENCY,
            rtt_ms=rtt_ms,
            bloat_ms=None,
            grade=None,
        )

    grade = grade_for_bloat(float(bloat_ms or 0.0))
    severite = GRADE_SEVERITY[grade]
    score = charge
    basis = BASIS_LOAD

    if latence is not None:
        # Le maillon FAIBLE fait la QoE : on garde la pire des deux composantes.
        score = min(score, latence)
        severite = _pire(severite, qoe_severity(latence))
        basis = BASIS_COMPOSITE

    return QoeScore(
        score=score,
        severity=severite,
        basis=basis,
        rtt_ms=rtt_ms,
        bloat_ms=float(bloat_ms or 0.0),
        grade=grade,
    )


def qoe_from_verdict(
    verdict: BufferbloatVerdict | None, *, rtt_ms: float | None = None
) -> QoeScore | None:
    """Raccourci : la note composite d'un verdict de bufferbloat.

    Le verdict porte deja la latence de reference (``idle_ms``) ; ``rtt_ms``
    permet de lui substituer une autre mesure (le p90 d'un pas de temps, par
    exemple, pour la heatmap).
    """
    if verdict is None:
        return compute_qoe(rtt_ms=rtt_ms)
    return compute_qoe(
        rtt_ms=verdict.idle_ms if rtt_ms is None else rtt_ms,
        bloat_ms=verdict.bloat_ms,
    )
