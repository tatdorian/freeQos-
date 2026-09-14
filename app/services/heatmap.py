"""Heatmap executif : QoE, RTT et utilisation dans le temps, en bandes de
cellules colorees facon LibreQoS.

Fonction PURE (pas de base, pas de reseau) : elle prend des points deja agreges
par pas de temps et produit les bandes pretes a afficher, alignees sur un axe de
temps fixe (une colonne par pas). Le depot lui fournit les points, l'API la
renvoie telle quelle. Tout est donc testable sans infrastructure.

LA LIGNE QoE N'EST PLUS UN PROXY LATENCE
----------------------------------------
Elle l'a longtemps ete : une fonction affine du RTT brut, faute de latence sous
charge. Chaque pas de temps porte desormais les echantillons ``(rtt, charge)``
des abonnes vus pendant ce pas ; on les passe a ``compute_bufferbloat`` — la
MEME correlation que la note A+..F par abonne — puis a ``compute_qoe``, la meme
fonction que la boucle fermee de la phase 4. Un pas ou les abonnes charges
pinguent nettement plus haut que les abonnes au repos vire donc a l'ambre meme
si le RTT median, lui, reste flatteur.

Quand un pas n'a pas assez d'echantillons pour conclure (parc minuscule, sonde
RTT a peine demarree), la cellule retombe sur le proxy latence et le DIT :
``basis="latency"``. Un repli annonce vaut mieux qu'un chiffre qu'on croit
mesure.

Convention de couleur commune a l'interface : vert (ok) / ambre (warn) /
rouge (crit), et "none" pour un pas sans mesure -- jamais du vert par defaut.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services.bufferbloat import compute_bufferbloat
from app.services.qoe import compute_qoe, qoe_severity

__all__ = [
    "build_heatmap",
    "qoe_severity",
    "rtt_severity",
    "util_severity",
]


def rtt_severity(ms: float | None) -> str:
    if ms is None:
        return "none"
    if ms < 30:
        return "ok"
    if ms < 100:
        return "warn"
    return "crit"


def util_severity(pct: float | None) -> str:
    if pct is None:
        return "none"
    if pct < 70:
        return "ok"
    if pct < 90:
        return "warn"
    return "crit"


def _axis(
    minutes: int, buckets: int, bucket_seconds: int, *, now: datetime | None = None
) -> list[int]:
    """Bornes basses des ``buckets`` derniers pas, alignees sur l'epoch.

    Meme alignement que ``date_bin(interval, ts, 'epoch')`` cote SQL, pour que
    les points tombent dans la bonne colonne.
    """
    reference = now or datetime.now(tz=UTC)
    epoch = int(reference.timestamp())
    dernier = epoch - (epoch % bucket_seconds)
    return [dernier - (buckets - 1 - i) * bucket_seconds for i in range(buckets)]


def _echantillons(cell: dict[str, Any]) -> list[tuple[float | None, float | None]]:
    """Couples ``(rtt_ms, charge_bps)`` du pas de temps.

    Le depot rend deux tableaux PARALLELES (``rtt_samples`` / ``load_samples``),
    agreges dans le meme ordre par la meme requete : un abonne = un indice. Des
    tableaux de longueurs differentes signeraient une requete modifiee d'un cote
    seulement, on tronque alors plutot que de correler des abonnes entre eux.
    """
    rtts = cell.get("rtt_samples") or []
    charges = cell.get("load_samples") or []
    return [
        (float(rtt), float(charge))
        for rtt, charge in zip(rtts, charges, strict=False)
        if rtt is not None and charge is not None
    ]


def build_heatmap(
    points: list[dict[str, Any]],
    *,
    minutes: int,
    buckets: int,
    bucket_seconds: int,
    reference_down_bps: float = 0.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    par_epoch = {}
    for point in points:
        bucket = point.get("bucket")
        if bucket is None:
            continue
        par_epoch[int(bucket.timestamp())] = point

    axis = _axis(minutes, buckets, bucket_seconds, now=now)

    qoe_cells: list[dict[str, Any]] = []
    rtt_cells: list[dict[str, Any]] = []
    util_cells: list[dict[str, Any]] = []
    for epoch in axis:
        ts = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
        cell = par_epoch.get(epoch)
        if cell is None:
            qoe_cells.append({"ts": ts, "value": None, "severity": "none"})
            rtt_cells.append({"ts": ts, "value": None, "severity": "none"})
            util_cells.append({"ts": ts, "value": None, "severity": "none"})
            continue

        rtt_p90 = cell.get("rtt_p90")
        rtt_p90 = float(rtt_p90) if rtt_p90 is not None else None
        # Latence SOUS CHARGE du pas : on compare, a cet instant, les abonnes
        # charges aux abonnes au repos. C'est la meme correlation que la note
        # A+..F par abonne, appliquee en travers du parc plutot que dans le temps.
        verdict = compute_bufferbloat(_echantillons(cell))
        note = compute_qoe(rtt_ms=rtt_p90, bloat_ms=verdict.bloat_ms if verdict else None)
        qoe_cells.append(
            {
                "ts": ts,
                "value": note.score if note else None,
                "severity": note.severity if note else "none",
                # D'ou vient la cellule : "composite"/"load" = latence sous charge
                # reellement mesuree, "latency" = repli sur le proxy RTT.
                "basis": note.basis if note else None,
                "bloat_ms": round(note.bloat_ms, 1) if note and note.bloat_ms is not None else None,
                "grade": note.grade if note else None,
            }
        )
        rtt_cells.append(
            {
                "ts": ts,
                "value": round(rtt_p90, 1) if rtt_p90 is not None else None,
                "severity": rtt_severity(rtt_p90),
            }
        )

        tx = float(cell.get("tx_sum") or 0.0)
        pct = (tx / reference_down_bps * 100.0) if reference_down_bps > 0 else None
        util_cells.append(
            {
                "ts": ts,
                "value": round(pct, 1) if pct is not None else None,
                "severity": util_severity(pct),
            }
        )

    return {
        "minutes": minutes,
        "bucket_seconds": bucket_seconds,
        "generated_at": (now or datetime.now(tz=UTC)),
        "rows": [
            {"key": "qoe", "label": "QoE", "unit": "", "cells": qoe_cells},
            {"key": "rtt", "label": "RTT p90", "unit": "ms", "cells": rtt_cells},
            {"key": "utilisation", "label": "Utilisation", "unit": "%", "cells": util_cells},
            {
                "key": "retransmits",
                "label": "Retransmissions TCP",
                "unit": "%",
                "unavailable": True,
                "reason": "Hors-bande : mesurer les retransmissions exige de voir les paquets.",
                "cells": [],
            },
        ],
    }
