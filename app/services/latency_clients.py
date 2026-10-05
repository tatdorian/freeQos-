"""Latence par CLIENT : l'experience que chaque abonne vit reellement.

La latence par segment dit OU se perd le temps ; elle ne dit pas QUI le subit.
Ici, chaque abonne a sa ligne : latence habituelle (mediane), moments penibles
(p95), gigue et perte de la derniere serie, et surtout la latence SOUS CHARGE
-- celle qui fait hacher un appel video quand quelqu'un telecharge a cote.

Fonction pure : des mesures en entree, une ligne et un verdict par abonne.
"""

from __future__ import annotations

from typing import Any

#: Seuils du verdict, alignes sur la legende de l'onglet (vert < 30 ms, rouge
#: > 100 ms) et sur le score d'experience (mauvais < 50).
BON_MS = 30.0
MAUVAIS_MS = 100.0
PERTE_MAUVAISE_PCT = 2.0
GONFLEMENT_NOTABLE_MS = 30.0
GONFLEMENT_GRAVE_MS = 150.0

BON = "good"
MOYEN = "fair"
MAUVAIS = "poor"
#: Au plafond de son forfait toute la periode : sa latence est celle de SA
#: propre file. Ni bon ni mauvais pour le reseau -- a part.
AU_PLAFOND = "limit"


def verdict(
    *,
    median_ms: float | None,
    p95_ms: float | None,
    loss_pct: float | None,
    bloat_ms: float | None,
    qoe_score: float | None,
    capped_samples: int = 0,
    capped_median_ms: float | None = None,
    at_cap_now: bool = False,
) -> tuple[str | None, list[str]]:
    """Le ressenti du client, et ce qui le degrade, en clair.

    UTILISER TOUT SON FORFAIT N'EST PAS UN PROBLEME DE RESEAU. Les mesures
    prises quand le client tourne a sa limite sont deja ecartees de la mediane
    (cf. AU_PLAFOND_SQL) ; un client au plafond toute la periode est classe a
    part, et une serie de pings perdue pendant qu'il est au plafond ne compte
    pas contre le reseau : ce sont ses propres paquets qui les retardent.
    """
    if median_ms is None and qoe_score is None:
        if capped_samples:
            texte = "at its plan limit the whole period: latency of its own queue"
            if capped_median_ms is not None:
                texte += f" ({capped_median_ms:.0f} ms)"
            return AU_PLAFOND, [texte + ", not a network issue"]
        return None, []
    if at_cap_now and loss_pct is not None and loss_pct >= PERTE_MAUVAISE_PCT:
        # Pings perdus derriere son propre trafic : on ne les compte pas.
        loss_pct = None
    motifs: list[str] = []
    grave = False
    if median_ms is not None and median_ms > MAUVAIS_MS:
        motifs.append(f"high latency ({median_ms:.0f} ms)")
        grave = True
    elif median_ms is not None and median_ms > BON_MS:
        motifs.append(f"latency {median_ms:.0f} ms")
    if p95_ms is not None and median_ms is not None and p95_ms > max(MAUVAIS_MS, 3 * median_ms):
        motifs.append(f"spikes up to {p95_ms:.0f} ms")
    if loss_pct is not None and loss_pct > 0:
        motifs.append(f"{loss_pct:.0f}% packet loss")
        grave = grave or loss_pct >= PERTE_MAUVAISE_PCT
    if bloat_ms is not None and bloat_ms >= GONFLEMENT_NOTABLE_MS:
        # Meme lecture que le score d'experience : un gonflement notable
        # degrade, seul un gonflement massif rend l'experience mauvaise.
        motifs.append(f"+{bloat_ms:.0f} ms under load (bufferbloat)")
        grave = grave or bloat_ms >= GONFLEMENT_GRAVE_MS
    if qoe_score is not None and qoe_score < 50:
        grave = True
    if grave:
        return MAUVAIS, motifs
    return (MOYEN if motifs else BON), motifs


def build_rows(
    latences: list[dict[str, Any]],
    charge: dict[int, dict[str, Any]],
    series: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Une ligne par abonne mesure, le pire ressenti en tete."""
    lignes: list[dict[str, Any]] = []
    for lat in latences:
        sid = int(lat["subscriber_id"])
        sous_charge = charge.get(sid) or {}
        serie = series.get(sid) or {}
        qoe = sous_charge.get("qoe") or {}
        mediane = _f(lat.get("median_ms"))
        p95 = _f(lat.get("p95_ms"))
        perte = _f(serie.get("loss_pct"))
        gonflement = _f(sous_charge.get("bloat_ms"))
        score = _f(qoe.get("score"))
        plafonnes = int(lat.get("capped_samples") or 0)
        etat, motifs = verdict(
            median_ms=mediane,
            p95_ms=p95,
            loss_pct=perte,
            bloat_ms=gonflement,
            qoe_score=score,
            capped_samples=plafonnes,
            capped_median_ms=_f(lat.get("capped_median_ms")),
            at_cap_now=bool(lat.get("at_cap_now")),
        )
        if plafonnes and etat not in (AU_PLAFOND, None):
            motifs.append(f"{plafonnes} measure(s) at its plan limit set aside")
        lignes.append(
            {
                "subscriber_id": sid,
                "login": lat.get("login"),
                "kind": lat.get("kind"),
                "pop_name": lat.get("pop_name"),
                "samples": int(lat.get("samples") or 0),
                "median_ms": _arrondi(mediane),
                "p95_ms": _arrondi(p95),
                "best_ms": _arrondi(_f(lat.get("best_ms"))),
                "jitter_ms": _arrondi(_f(serie.get("jitter_ms"))),
                "loss_pct": _arrondi(perte),
                "loaded_ms": _arrondi(_f(sous_charge.get("loaded_ms"))),
                "bloat_ms": _arrondi(gonflement),
                "grade": sous_charge.get("grade"),
                "qoe_score": _arrondi(score),
                "last_at": lat.get("last_at"),
                "experience": etat,
                "reasons": motifs,
                "capped_samples": plafonnes,
                "at_cap_now": bool(lat.get("at_cap_now")),
            }
        )
    rang = {MAUVAIS: 0, MOYEN: 1, AU_PLAFOND: 2, BON: 3, None: 4}
    lignes.sort(key=lambda x: (rang.get(x["experience"], 3), -(x["median_ms"] or 0)))
    return lignes


def summary(lignes: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "measured": len(lignes),
        BON: sum(1 for x in lignes if x["experience"] == BON),
        MOYEN: sum(1 for x in lignes if x["experience"] == MOYEN),
        MAUVAIS: sum(1 for x in lignes if x["experience"] == MAUVAIS),
        AU_PLAFOND: sum(1 for x in lignes if x["experience"] == AU_PLAFOND),
    }


def _f(valeur: Any) -> float | None:
    try:
        return None if valeur is None else float(valeur)
    except (TypeError, ValueError):
        return None


def _arrondi(valeur: float | None) -> float | None:
    return None if valeur is None else round(valeur, 1)
