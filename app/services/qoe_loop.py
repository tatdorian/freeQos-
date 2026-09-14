"""Boucle fermee QoE (phase 4) : la DECISION, isolee et pure.

CE QUE FAIT CETTE BOUCLE
------------------------
Jusqu'ici, la seule grandeur qui refermait une boucle etait la capacite backhaul
mesuree chez UISP : le planificateur pose la file du lien parent a
``mesure * facteur de securite``. C'est la boucle centrale lente pour le goulot
RADIO, et elle marche.

Ce qu'elle ne voit pas : un secteur dont la latence GONFLE sous charge alors que
la radio, elle, annonce toujours sa capacite. C'est le cas classique du buffer
d'equipement trop gros — la capacite brute est la, l'experience ne l'est pas.
Le signal qui le dit existe deja (``compute_bufferbloat``, puis ``compute_qoe``)
mais n'etait lu que par un tableau de bord.

Cette boucle ferme ce circuit-la : quand la QoE d'un SECTEUR se degrade, on
RESSERRE la file du lien de ce secteur d'un cran, pour que la file d'attente se
reforme dans CAKE, ou on la controle, plutot que dans un buffer radio ou on ne
peut rien. Quand elle se retablit durablement, on relache, un cran a la fois.

TROIS PRINCIPES, ASSUMES
------------------------
1. **On ne touche jamais au plan souscrit de l'abonne.** Un abonne n'est pas
   responsable du bufferbloat de son secteur, et lui retirer le debit qu'il paie
   serait la mauvaise reponse. Ce qui bouge, c'est l'enveloppe PARTAGEE du
   secteur ; CAKE arbitre ensuite entre les circuits, comme d'habitude.
2. **Un abonne degrade ne suffit pas.** Un seul abonne qui gonfle, c'est SON
   dernier km (CPE, wifi domestique, pare-feu). C'est la correlation entre
   PLUSIEURS abonnes du meme secteur qui designe le secteur — d'ou le seuil
   ``min_degraded``.
3. **On resserre vite, on relache lentement.** Une degradation agit des le cycle
   suivant ; un retour a la normale doit tenir ``recovery_cycles`` cycles avant
   de rendre un cran. Sans cette asymetrie la boucle oscillerait : chaque
   desserrage ramenant la saturation qui l'avait provoque.

Ce module est une FONCTION PURE : pas de base, pas de reseau, pas d'horloge. On
lui donne des scores et un etat, il rend un verdict. C'est le service
(``ShapingService.adjust_for_qoe``) qui lit, persiste, planifie et applique.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# Ce que la boucle a decide pour un secteur, sur ce cycle.
ACTION_TIGHTEN = "tighten"  # QoE degradee : on resserre d'un cran
ACTION_RELAX = "relax"  # retablie assez longtemps : on rend un cran
ACTION_HOLD = "hold"  # rien a faire (sain sans resserrage, ou delai de garde)
ACTION_FLOOR = "floor"  # degrade mais deja au plancher : on ne descend plus
ACTION_UNKNOWN = "unknown"  # pas assez de mesures pour conclure

# Un resserrage ne change rien tant qu'il reste dans le bruit d'arrondi.
EPSILON = 1e-6


@dataclass(slots=True)
class SectorState:
    """Etat de controle d'un secteur, tel que persiste entre deux cycles."""

    trim_factor: float = 1.0
    healthy_cycles: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> SectorState:
        if not row:
            return cls()
        return cls(
            trim_factor=float(row.get("trim_factor") or 1.0),
            healthy_cycles=int(row.get("healthy_cycles") or 0),
        )


@dataclass(slots=True)
class SectorVerdict:
    """Ce que la boucle a decide pour un secteur, et POURQUOI.

    ``reason`` est ecrit pour etre lisible tel quel dans le journal et dans
    l'interface : une boucle qui resserre sans dire pourquoi est une boucle que
    personne n'osera laisser active.
    """

    sector_key: str
    link_key: str
    action: str
    reason: str
    scored: int
    degraded: int
    worst_score: float | None
    trim_before: float
    trim_after: float
    healthy_cycles: int
    logins: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Le resserrage a-t-il bouge ? C'est LUI qui declenche un plan."""
        return abs(self.trim_after - self.trim_before) > EPSILON

    def as_dict(self) -> dict[str, Any]:
        return {
            "sector_key": self.sector_key,
            "link_key": self.link_key,
            "action": self.action,
            "reason": self.reason,
            "scored": self.scored,
            "degraded": self.degraded,
            "worst_score": self.worst_score,
            "trim_before": round(self.trim_before, 4),
            "trim_after": round(self.trim_after, 4),
            "healthy_cycles": self.healthy_cycles,
            "logins": self.logins,
        }


def decide_sector(
    *,
    sector_key: str,
    link_key: str,
    scores: Sequence[float],
    state: SectorState,
    threshold: float,
    min_degraded: int,
    step: float,
    floor: float,
    recovery_cycles: int,
    logins: Sequence[str] = (),
) -> SectorVerdict:
    """Decide ce qu'il advient d'un secteur, a partir des scores de ses abonnes.

    ``scores`` ne contient QUE des abonnes reellement notes : un abonne
    silencieux ou jamais sonde n'a pas de note, et il n'a pas a compter — ni
    comme sain, ni comme degrade.
    """
    avant = round(state.trim_factor, 4)
    notes = [float(s) for s in scores]
    degrades = [s for s in notes if s < threshold]
    pire = min(notes) if notes else None

    def verdict(action: str, reason: str, trim_after: float, healthy_cycles: int) -> SectorVerdict:
        return SectorVerdict(
            sector_key=sector_key,
            link_key=link_key,
            action=action,
            reason=reason,
            scored=len(notes),
            degraded=len(degrades),
            worst_score=pire,
            trim_before=avant,
            trim_after=trim_after,
            healthy_cycles=healthy_cycles,
            logins=list(logins),
        )

    if not notes:
        # Aucune mesure : on ne touche a rien, et surtout on ne fait pas avancer
        # le compteur de retablissement. Un secteur dont la sonde s'est tue
        # n'est pas un secteur qui va bien.
        return verdict(
            ACTION_UNKNOWN,
            "aucun abonne note sur la fenetre : rien a decider",
            avant,
            state.healthy_cycles,
        )

    if len(degrades) >= min_degraded:
        if avant <= floor + EPSILON:
            return verdict(
                ACTION_FLOOR,
                f"{len(degrades)}/{len(notes)} abonne(s) sous {threshold:g} mais le "
                f"secteur est deja au plancher ({floor:.0%}) : le goulot n'est pas "
                "ici, il faut de la capacite",
                floor,
                0,
            )
        apres = round(max(floor, avant - step), 4)
        return verdict(
            ACTION_TIGHTEN,
            f"{len(degrades)}/{len(notes)} abonne(s) sous {threshold:g} "
            f"(pire {pire:g}) : partage du secteur resserre a {apres:.0%}",
            apres,
            0,
        )

    # Secteur sain.
    if avant >= 1.0 - EPSILON:
        return verdict(ACTION_HOLD, "QoE dans les clous, aucun resserrage en cours", 1.0, 0)

    cycles = state.healthy_cycles + 1
    if cycles < recovery_cycles:
        return verdict(
            ACTION_HOLD,
            f"QoE retablie depuis {cycles}/{recovery_cycles} cycle(s) : "
            f"le resserrage a {avant:.0%} est maintenu le temps du delai de garde",
            avant,
            cycles,
        )

    apres = round(min(1.0, avant + step), 4)
    return verdict(
        ACTION_RELAX,
        f"QoE retablie depuis {recovery_cycles} cycles : partage du secteur rendu a {apres:.0%}",
        apres,
        0,
    )
