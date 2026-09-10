"""Heatmap executif : QoE, RTT et utilisation dans le temps, en bandes de
cellules colorees facon LibreQoS.

Fonction PURE (pas de base, pas de reseau) : elle prend des points deja agreges
par pas de temps et produit les bandes pretes a afficher, alignees sur un axe de
temps fixe (une colonne par pas). Le depot lui fournit les points, l'API la
renvoie telle quelle. Tout est donc testable sans infrastructure.

Convention de couleur commune a l'interface : vert (ok) / ambre (warn) /
rouge (crit), et "none" pour un pas sans mesure — jamais du vert par defaut.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def rtt_severity(ms: float | None) -> str:
    if ms is None:
        return "none"
    if ms < 30:
        return "ok"
    if ms < 100:
        return "warn"
    return "crit"


def qoe_from_rtt(ms: float | None) -> float | None:
    """Score de QoE 0..100 derive de la latence.

    Faute de latence sous charge en continu (hors-bande), on approxime la QoE
    par le RTT : imperceptible en dessous de 10 ms, il se degrade ensuite. C'est
    le meme esprit que le QoO de LibreQoS, mais assume comme un proxy latence.
    """
    if ms is None:
        return None
    score = 100.0 - max(0.0, ms - 10.0) * 0.6
    return round(max(0.0, min(100.0, score)), 0)


def qoe_severity(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= 80:
        return "ok"
    if score >= 50:
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

    qoe_cells, rtt_cells, util_cells = [], [], []
    for epoch in axis:
        ts = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
        point = par_epoch.get(epoch)
        if point is None:
            qoe_cells.append({"ts": ts, "value": None, "severity": "none"})
            rtt_cells.append({"ts": ts, "value": None, "severity": "none"})
            util_cells.append({"ts": ts, "value": None, "severity": "none"})
            continue

        rtt_p90 = point.get("rtt_p90")
        rtt_p90 = float(rtt_p90) if rtt_p90 is not None else None
        score = qoe_from_rtt(rtt_p90)
        qoe_cells.append({"ts": ts, "value": score, "severity": qoe_severity(score)})
        rtt_cells.append(
            {
                "ts": ts,
                "value": round(rtt_p90, 1) if rtt_p90 is not None else None,
                "severity": rtt_severity(rtt_p90),
            }
        )

        tx = float(point.get("tx_sum") or 0.0)
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
