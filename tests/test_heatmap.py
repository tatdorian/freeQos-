"""Heatmap executif : bandes QoE / RTT / utilisation dans le temps."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.heatmap import (
    build_heatmap,
    qoe_severity,
    rtt_severity,
    util_severity,
)
from app.services.qoe import qoe_from_rtt


def test_severites_rtt() -> None:
    assert rtt_severity(None) == "none"
    assert rtt_severity(10) == "ok"
    assert rtt_severity(50) == "warn"
    assert rtt_severity(200) == "crit"


def test_le_proxy_latence_reste_disponible_comme_repli() -> None:
    """Conserve, mais comme REPLI seulement : la ligne QoE de la heatmap passe
    desormais par le score composite (cf. test_qoe.py)."""
    assert qoe_from_rtt(None) is None
    assert qoe_from_rtt(5) == 100  # imperceptible
    assert qoe_from_rtt(10) == 100
    assert qoe_from_rtt(200) < 20  # injouable
    assert qoe_severity(qoe_from_rtt(10)) == "ok"
    assert qoe_severity(qoe_from_rtt(200)) == "crit"


def test_severite_utilisation() -> None:
    assert util_severity(None) == "none"
    assert util_severity(10) == "ok"
    assert util_severity(80) == "warn"
    assert util_severity(95) == "crit"


def test_axe_de_temps_fixe_et_trous_marques() -> None:
    """L'axe a toujours `buckets` colonnes ; un pas sans mesure est 'none'."""
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    bucket_s = 60
    # Une seule mesure, dans l'avant-dernier pas.
    dernier = int(now.timestamp()) - (int(now.timestamp()) % bucket_s)
    point_ts = datetime.fromtimestamp(dernier - bucket_s, tz=UTC)
    heat = build_heatmap(
        [{"bucket": point_ts, "rtt_p50": 12.0, "rtt_p90": 40.0, "tx_sum": 50_000_000.0}],
        minutes=5,
        buckets=5,
        bucket_seconds=bucket_s,
        reference_down_bps=100_000_000.0,
        now=now,
    )
    rtt = next(r for r in heat["rows"] if r["key"] == "rtt")
    assert len(rtt["cells"]) == 5
    # Le pas rempli porte la valeur et la couleur, les autres sont 'none'.
    remplis = [c for c in rtt["cells"] if c["severity"] != "none"]
    assert len(remplis) == 1
    assert remplis[0]["value"] == 40.0
    assert remplis[0]["severity"] == "warn"
    util = next(r for r in heat["rows"] if r["key"] == "utilisation")
    rempli_util = [c for c in util["cells"] if c["severity"] != "none"][0]
    assert rempli_util["value"] == 50.0  # 50 Mbps / 100 Mbps
    assert rempli_util["severity"] == "ok"


def test_ligne_retransmissions_marquee_indisponible() -> None:
    heat = build_heatmap(
        [], minutes=5, buckets=5, bucket_seconds=60, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    retr = next(r for r in heat["rows"] if r["key"] == "retransmits")
    assert retr["unavailable"] is True
    assert "hors-bande" in retr["reason"].lower()


def test_utilisation_none_sans_reference() -> None:
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    dernier = int(now.timestamp()) - (int(now.timestamp()) % 60)
    heat = build_heatmap(
        [{"bucket": datetime.fromtimestamp(dernier, tz=UTC), "rtt_p90": 20.0, "tx_sum": 1e6}],
        minutes=5,
        buckets=5,
        bucket_seconds=60,
        reference_down_bps=0.0,
        now=now,
    )
    util = next(r for r in heat["rows"] if r["key"] == "utilisation")
    assert all(c["severity"] == "none" for c in util["cells"])


# ------------------------------------------- la ligne QoE n'est plus un proxy
def _bucket(now: datetime, bucket_s: int = 60) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % bucket_s), tz=UTC)


def test_la_qoe_du_pas_vient_de_la_latence_sous_charge() -> None:
    """Le RTT median est flatteur, mais les abonnes CHARGES pinguent haut.

    C'est exactement le cas que l'ancien proxy manquait : une moitie du parc au
    repos a 10 ms tire le p90 vers le bas, pendant que les abonnes qui
    televersent sont a 260 ms. La cellule doit virer au rouge.
    """
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    bucket = _bucket(now)
    heat = build_heatmap(
        [
            {
                "bucket": bucket,
                "rtt_p90": 30.0,
                "tx_sum": 0.0,
                # Six abonnes : quatre au repos a ~10 ms, deux charges a ~260 ms.
                "rtt_samples": [9.0, 10.0, 11.0, 12.0, 255.0, 265.0],
                "load_samples": [0.0, 0.0, 1_000.0, 2_000.0, 80e6, 90e6],
            }
        ],
        minutes=5,
        buckets=5,
        bucket_seconds=60,
        now=now,
    )

    cellule = next(
        c
        for c in next(r for r in heat["rows"] if r["key"] == "qoe")["cells"]
        if c["severity"] != "none"
    )
    assert cellule["basis"] == "composite"
    assert cellule["grade"] == "F"
    assert cellule["severity"] == "crit"
    assert cellule["bloat_ms"] is not None and cellule["bloat_ms"] > 200


def test_sans_echantillons_la_cellule_replie_sur_le_proxy_et_le_dit() -> None:
    """Parc minuscule, sonde a peine demarree : on ne peut pas correler.

    On retombe alors sur le proxy latence — mais la cellule l'annonce
    (``basis="latency"``), elle ne se fait pas passer pour une mesure sous charge.
    """
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    heat = build_heatmap(
        [{"bucket": _bucket(now), "rtt_p90": 20.0, "tx_sum": 1e6}],
        minutes=5,
        buckets=5,
        bucket_seconds=60,
        now=now,
    )

    cellule = next(
        c
        for c in next(r for r in heat["rows"] if r["key"] == "qoe")["cells"]
        if c["severity"] != "none"
    )
    assert cellule["basis"] == "latency"
    assert cellule["grade"] is None
    assert cellule["value"] == qoe_from_rtt(20.0)
