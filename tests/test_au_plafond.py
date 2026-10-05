"""Utiliser tout son forfait n'est pas un probleme de reseau.

Constate : un client en bandwidth-test a 100 % de sa limite etait note
« poor », latence rouge, pings perdus -- alors que c'etait sa propre file qui
le retardait, pas le reseau."""

from __future__ import annotations

from app.services.latency_clients import AU_PLAFOND, MAUVAIS, verdict


def test_au_plafond_toute_la_periode_il_est_classe_a_part() -> None:
    etat, motifs = verdict(
        median_ms=None,
        p95_ms=None,
        loss_pct=None,
        bloat_ms=None,
        qoe_score=None,
        capped_samples=120,
        capped_median_ms=139.0,
    )
    assert etat == AU_PLAFOND
    assert "not a network issue" in motifs[0] and "139 ms" in motifs[0]


def test_des_pings_perdus_au_plafond_ne_comptent_pas() -> None:
    etat, _ = verdict(
        median_ms=10.0,
        p95_ms=12.0,
        loss_pct=100.0,
        bloat_ms=None,
        qoe_score=95.0,
        at_cap_now=True,
    )
    assert etat != MAUVAIS


def test_des_pings_perdus_sans_plafond_sont_un_vrai_probleme() -> None:
    etat, motifs = verdict(
        median_ms=10.0,
        p95_ms=12.0,
        loss_pct=100.0,
        bloat_ms=None,
        qoe_score=95.0,
        at_cap_now=False,
    )
    assert etat == MAUVAIS
    assert any("loss" in m for m in motifs)
