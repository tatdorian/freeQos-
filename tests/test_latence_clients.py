"""Latence par CLIENT : chaque abonne, son ressenti, le pire en tete.

DEMANDE EXPLICITE : "la latence, je veux que tu la mesures en fonction des
clients pour avoir un bon retour experience utilisateur". La latence par
segment dit ou se perd le temps ; elle ne dit pas qui le subit.
"""

from __future__ import annotations

from app.services.latency_clients import BON, MAUVAIS, MOYEN, build_rows, summary, verdict


def test_un_client_rapide_et_stable_est_bon() -> None:
    assert verdict(median_ms=12, p95_ms=20, loss_pct=0, bloat_ms=5, qoe_score=95) == (BON, [])


def test_la_latence_sous_charge_degrade_puis_rend_l_experience_mauvaise() -> None:
    etat, motifs = verdict(median_ms=15, p95_ms=40, loss_pct=0, bloat_ms=80, qoe_score=60)
    assert etat == MOYEN
    assert any("under load" in m for m in motifs)
    assert verdict(median_ms=15, p95_ms=40, loss_pct=0, bloat_ms=200, qoe_score=None)[0] == MAUVAIS
    # Le verdict suit le score : sous 50, c'est mauvais.
    assert verdict(median_ms=15, p95_ms=40, loss_pct=0, bloat_ms=80, qoe_score=45)[0] == MAUVAIS


def test_la_perte_et_les_pics_sont_nommes() -> None:
    etat, motifs = verdict(median_ms=40, p95_ms=300, loss_pct=1, bloat_ms=None, qoe_score=None)
    assert etat == MOYEN
    assert "spikes up to 300 ms" in motifs and "1% packet loss" in motifs


def test_sans_mesure_pas_de_verdict_invente() -> None:
    assert verdict(median_ms=None, p95_ms=None, loss_pct=None, bloat_ms=None, qoe_score=None) == (
        None,
        [],
    )


def test_le_pire_ressenti_en_tete_et_le_resume() -> None:
    latences = [
        {"subscriber_id": 1, "login": "rapide", "median_ms": 10, "p95_ms": 15, "samples": 30},
        {"subscriber_id": 2, "login": "lent", "median_ms": 140, "p95_ms": 220, "samples": 30},
        {"subscriber_id": 3, "login": "gonfle", "median_ms": 20, "p95_ms": 50, "samples": 30},
    ]
    charge = {3: {"loaded_ms": 110, "bloat_ms": 90, "grade": "D", "qoe": {"score": 45}}}
    series = {2: {"jitter_ms": 12.3, "loss_pct": 0}}
    lignes = build_rows(latences, charge, series)

    assert [x["login"] for x in lignes] == ["lent", "gonfle", "rapide"]
    assert lignes[0]["jitter_ms"] == 12.3
    assert lignes[1]["loaded_ms"] == 110 and lignes[1]["qoe_score"] == 45
    assert summary(lignes) == {"measured": 3, BON: 1, MOYEN: 0, MAUVAIS: 2, "limit": 0}
