"""Intelligence abonnes : qui risque de partir, qui est pret a monter en gamme.

Deux listes que le reste de l'interface ne donnait pas, et qui se decident
avec des donnees deja en base -- debit, plan, latence sous charge :

- RISQUE DE DEPART : une experience mauvaise qui dure, un usage qui s'effondre
  d'une periode a l'autre, ou un abonne declare qui ne consomme plus rien. Ce
  sont les trois signes qu'un client est en train de partir, bien avant qu'il
  n'appelle pour resilier.
- PRET POUR UN PLAN SUPERIEUR : il vit a son plafond, et son experience reste
  bonne -- c'est donc le PLAN qui le limite, pas le reseau. Un abonne qui sature
  son plan ET dont la latence gonfle est d'abord un probleme reseau : le lui
  vendre plus cher ne reglerait rien, il n'est pas dans cette liste.

Fonction pure : des lignes en entree, un verdict par abonne en sortie.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: Part du temps passee a >= 90 % du plan au-dela de laquelle un abonne est "a
#: l'etroit" dans son offre.
PLAFOND_PART = 0.15
#: Score QoE (0..100) sous lequel l'experience est mauvaise.
QOE_MAUVAISE = 50.0
#: Chute d'usage (periode courante / precedente) qui signale un depart.
CHUTE = 0.30
#: En dessous, l'usage precedent etait trop faible pour que sa chute dise quoi
#: que ce soit (10 kbps de moyenne).
USAGE_SIGNIFICATIF_BPS = 10_000.0
#: Un abonne sans trafic depuis ce nombre de jours est "silencieux".
SILENCE_JOURS = 3.0


def classify(
    row: dict[str, Any], qoe: dict[str, Any] | None, *, now: datetime | None = None
) -> dict[str, Any]:
    maintenant = now or datetime.now(tz=UTC)
    echantillons = int(row.get("samples") or 0)
    plafond = int(row.get("capped_samples") or 0)
    part_plafond = plafond / echantillons if echantillons else 0.0
    moyen = float(row.get("avg_down_bps") or 0.0)
    avant = float(row.get("prev_avg_down_bps") or 0.0)
    score = float(qoe["score"]) if qoe and qoe.get("score") is not None else None

    risques: list[str] = []
    if score is not None and score < QOE_MAUVAISE:
        risques.append(f"poor experience (score {score:.0f}/100)")
    if avant >= USAGE_SIGNIFICATIF_BPS and moyen < avant * CHUTE:
        risques.append(f"usage down {100 * (1 - moyen / avant):.0f}% vs previous period")
    dernier = row.get("last_traffic_at")
    silence_j = (maintenant - dernier).total_seconds() / 86400 if dernier else None
    if avant >= USAGE_SIGNIFICATIF_BPS and (silence_j is None or silence_j >= SILENCE_JOURS):
        risques.append(
            "no traffic for " + (f"{silence_j:.0f} days" if silence_j is not None else "the period")
        )

    montee: list[str] = []
    if row.get("plan_down_mbps") and part_plafond >= PLAFOND_PART:
        if score is None or score >= QOE_MAUVAISE:
            montee.append(f"at its plan ceiling {100 * part_plafond:.0f}% of the time")

    statut = "at_risk" if risques else "upgrade" if montee else "healthy"
    return {
        "subscriber_id": row.get("subscriber_id"),
        "login": row.get("login"),
        "kind": row.get("kind"),
        "pop_name": row.get("pop_name"),
        "plan_down_mbps": row.get("plan_down_mbps"),
        "plan_up_mbps": row.get("plan_up_mbps"),
        "avg_down_bps": moyen,
        "prev_avg_down_bps": avant,
        "peak_down_bps": float(row.get("peak_down_bps") or 0.0),
        "ceiling_share": round(part_plafond, 3),
        "qoe_score": score,
        "qoe_grade": qoe.get("grade") if qoe else None,
        "last_traffic_at": dernier,
        "status": statut,
        "reasons": risques or montee,
    }


def summarise(lignes: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "at_risk": sum(1 for x in lignes if x["status"] == "at_risk"),
        "upgrade": sum(1 for x in lignes if x["status"] == "upgrade"),
        "healthy": sum(1 for x in lignes if x["status"] == "healthy"),
    }


# =========================================================================
# Combien d'abonnes de plus un site / une AP peut-il prendre ?
# =========================================================================

#: Occupation cible a la pointe : au-dela, la latence sous charge monte (les
#: files se remplissent) bien avant que le lien ne soit plein.
OCCUPATION_CIBLE = 0.80
#: Part d'abonnes en mauvaise experience au-dela de laquelle le site n'a plus
#: de place, quelle que soit la marge apparente du lien.
QOE_SATURE_PART = 0.20


def ap_room(
    *,
    capacity_mbps: float | None,
    peak_bps: float | None,
    subscribers: int,
    poor_share: float | None,
) -> dict[str, Any]:
    """La place restante, en abonnes, avant que la QoE ne souffre.

    Chaque abonne ajoute, a la pointe, ce qu'ajoutent en moyenne ceux qui y sont
    deja (pointe observee / nombre d'abonnes) : c'est la mesure du site, pas un
    ratio de survente theorique. La place est ce qui reste sous 80 % de la
    capacite a la pointe, divise par cette contribution. Un site ou deja un
    abonne sur cinq a une mauvaise experience n'a plus de place, marge ou non.
    """
    if not capacity_mbps:
        return {
            "room": None,
            "reason": "capacity unknown: add its antenna or set the link bandwidth",
        }
    if poor_share is not None and poor_share >= QOE_SATURE_PART:
        return {
            "room": 0,
            "reason": f"{100 * poor_share:.0f}% of its subscribers already have a poor experience",
        }
    cible = capacity_mbps * 1e6 * OCCUPATION_CIBLE
    pointe = float(peak_bps or 0.0)
    if subscribers <= 0 or pointe <= 0:
        return {"room": None, "reason": "no busy-hour measurement yet"}
    par_abonne = pointe / subscribers
    reste = cible - pointe
    if reste <= 0:
        return {
            "room": 0,
            "reason": (
                f"busy-hour peak already at {100 * pointe / (capacity_mbps * 1e6):.0f}% of capacity"
            ),
        }
    return {
        "room": int(reste // par_abonne),
        "per_subscriber_mbps": round(par_abonne / 1e6, 2),
        "reason": f"each subscriber adds ~{par_abonne / 1e6:.1f} Mbps at the busy hour",
    }
