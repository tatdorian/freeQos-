"""Mesure du bufferbloat : la latence qui GONFLE quand le lien se remplit.

Le bufferbloat, c'est la latence supplementaire introduite par des files
d'attente trop grosses des qu'un lien sature. Un lien peut pinguer a 8 ms au
repos et grimper a 300 ms des qu'un abonne televerse : c'est invisible sur une
mesure de latence a vide, et pourtant c'est ce qui rend une visio ou un jeu
inutilisables. La seule facon honnete de le voir, c'est de comparer la latence
QUAND LE LIEN EST CHARGE a la latence QUAND IL NE L'EST PAS.

C'est exactement ce que fait un test type Waveform / DSLReports, mais en continu
et par abonne : on possede deja, dans ``subscriber_metrics``, le RTT
(sonde active ``/ping``) et le debit du meme echantillon. On n'a donc pas besoin
d'un test dedie — il suffit de correler ce qu'on collecte deja.

C'est aussi la brique qui manquait au score QoE (phase 3 du README) : la latence
sous charge s'obtient en rapprochant RTT et debit, precisement ce que fait ce
module.

Ce fichier est une FONCTION PURE, sans base ni reseau : il prend des
echantillons ``(rtt_ms, charge_bps)`` et rend une note. Le depot lui fournit les
echantillons, l'API la note. Tout est donc testable sans infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bareme de la latence AJOUTEE sous charge, en millisecondes. Inspire de l'echelle
# grand public (Waveform / DSLReports) pour que la note parle a tout le monde :
# A+ est imperceptible, F rend le temps reel impossible. La borne est le plafond
# INCLUS de la note (bloat <= borne).
GRADE_THRESHOLDS: list[tuple[float, str]] = [
    (5.0, "A+"),
    (30.0, "A"),
    (60.0, "B"),
    (100.0, "C"),
    (200.0, "D"),
]
WORST_GRADE = "F"

# Une note se traduit en severite pour la coloration de l'interface (vert /
# ambre / rouge), la meme convention que les jauges.
GRADE_SEVERITY: dict[str, str] = {
    "A+": "ok",
    "A": "ok",
    "B": "warn",
    "C": "warn",
    "D": "crit",
    "F": "crit",
}


def grade_for_bloat(bloat_ms: float) -> str:
    """Traduit une latence ajoutee (ms) en note A+..F."""
    for borne, note in GRADE_THRESHOLDS:
        if bloat_ms <= borne:
            return note
    return WORST_GRADE


def _quantile(valeurs_triees: list[float], q: float) -> float:
    """Quantile par interpolation lineaire. ``valeurs_triees`` doit etre trie."""
    if not valeurs_triees:
        raise ValueError("quantile d'une liste vide")
    if len(valeurs_triees) == 1:
        return valeurs_triees[0]
    q = min(1.0, max(0.0, q))
    position = q * (len(valeurs_triees) - 1)
    bas = int(position)
    haut = min(bas + 1, len(valeurs_triees) - 1)
    fraction = position - bas
    return valeurs_triees[bas] + (valeurs_triees[haut] - valeurs_triees[bas]) * fraction


@dataclass(slots=True)
class BufferbloatVerdict:
    grade: str
    severity: str
    idle_ms: float
    loaded_ms: float
    bloat_ms: float
    samples: int
    loaded_samples: int
    load_max_bps: float

    def as_dict(self) -> dict[str, object]:
        return {
            "grade": self.grade,
            "severity": self.severity,
            "idle_ms": round(self.idle_ms, 1),
            "loaded_ms": round(self.loaded_ms, 1),
            "bloat_ms": round(self.bloat_ms, 1),
            "samples": self.samples,
            "loaded_samples": self.loaded_samples,
            "load_max_bps": self.load_max_bps,
        }


def compute_bufferbloat(
    samples: list[tuple[float | None, float | None]],
    *,
    min_samples: int = 4,
    min_loaded_samples: int = 2,
    load_split_quantile: float = 0.6,
    idle_quantile: float = 0.2,
    loaded_quantile: float = 0.9,
) -> BufferbloatVerdict | None:
    """Deduit le bufferbloat d'echantillons ``(rtt_ms, charge_bps)``.

    Renvoie ``None`` quand on ne peut PAS conclure honnetement :

    * moins de ``min_samples`` echantillons exploitables ;
    * aucune charge (un abonne silencieux ne dit rien de son bufferbloat) ;
    * pas assez d'echantillons dans la tranche chargee pour distinguer le repos
      de la charge.

    Rendre ``None`` plutot qu'une note optimiste est volontaire : afficher "A+"
    sur un abonne qui n'a jamais rien televerse serait un faux positif rassurant.

    * ``idle_ms``   = latence de reference, bas quantile de TOUS les RTT (le
      plancher observe, robuste a un point aberrant, plutot que le strict min).
    * ``loaded_ms`` = haut quantile des RTT dans la tranche la plus chargee.
    * ``bloat_ms``  = ce que la charge ajoute, jamais negatif.
    """
    points = [
        (float(rtt), float(charge))
        for rtt, charge in samples
        if rtt is not None and charge is not None and rtt >= 0 and charge >= 0
    ]
    if len(points) < min_samples:
        return None

    charges = sorted(charge for _, charge in points)
    load_max = charges[-1]
    if load_max <= 0:
        return None

    seuil = _quantile(charges, load_split_quantile)
    # Un lien a charge constante (seuil == plancher) n'a pas de "tranche chargee"
    # distincte : on compare alors au-dessus vs au-dessous de la mediane.
    if seuil <= charges[0]:
        seuil = _quantile(charges, 0.5)

    charges_hautes = sorted(rtt for rtt, charge in points if charge >= seuil and charge > 0)
    if len(charges_hautes) < min_loaded_samples:
        return None

    tous_rtt = sorted(rtt for rtt, _ in points)
    idle_ms = _quantile(tous_rtt, idle_quantile)
    loaded_ms = _quantile(charges_hautes, loaded_quantile)
    bloat_ms = max(0.0, loaded_ms - idle_ms)
    grade = grade_for_bloat(bloat_ms)

    return BufferbloatVerdict(
        grade=grade,
        severity=GRADE_SEVERITY[grade],
        idle_ms=idle_ms,
        loaded_ms=loaded_ms,
        bloat_ms=bloat_ms,
        samples=len(points),
        loaded_samples=len(charges_hautes),
        load_max_bps=load_max,
    )


def summarize(verdicts: list[BufferbloatVerdict]) -> dict[str, object]:
    """Vue d'ensemble reseau : repartition des notes et pire cas.

    La distribution compte plus que la moyenne : dix abonnes en A+ et un en F,
    ce n'est pas "presque A", c'est un abonne dont la visio ne marche pas.
    """
    distribution: dict[str, int] = {}
    for note in [*[g for _, g in GRADE_THRESHOLDS], WORST_GRADE]:
        distribution[note] = 0
    pire: BufferbloatVerdict | None = None
    for verdict in verdicts:
        distribution[verdict.grade] = distribution.get(verdict.grade, 0) + 1
        if pire is None or verdict.bloat_ms > pire.bloat_ms:
            pire = verdict
    return {
        "measured": len(verdicts),
        "distribution": distribution,
        "worst_bloat_ms": round(pire.bloat_ms, 1) if pire else None,
    }
