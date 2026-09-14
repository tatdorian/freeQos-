"""Score de QoE composite (phase 3 terminee, phase 4 amorcee).

Ce que ces tests FIXENT, au-dela du calcul lui-meme : le fait que la heatmap
Executif et la boucle fermee lisent une SEULE fonction de score. Tant que
``compute_qoe`` est le seul chemin, l'ecran et le declencheur ne peuvent pas
diverger.
"""

from __future__ import annotations

import pytest

from app.services.bufferbloat import (
    GRADE_SEVERITY,
    BufferbloatVerdict,
    compute_bufferbloat,
    grade_for_bloat,
)
from app.services.qoe import (
    BASIS_COMPOSITE,
    BASIS_LATENCY,
    BASIS_LOAD,
    compute_qoe,
    qoe_from_bloat,
    qoe_from_rtt,
    qoe_from_verdict,
    qoe_severity,
)


# ------------------------------------------------------------------ nominal
def test_cas_nominal_lien_sain() -> None:
    """8 ms au repos, 3 ms de gonflement sous charge : rien a signaler."""
    note = compute_qoe(rtt_ms=8.0, bloat_ms=3.0)

    assert note is not None
    assert note.basis == BASIS_COMPOSITE
    assert note.grade == "A+"
    assert note.severity == "ok"
    assert note.score >= 90


def test_cas_nominal_bufferbloat_franc() -> None:
    """Meme latence a vide, mais 250 ms de gonflement : la visio ne passe plus.

    C'est precisement ce que l'ancien proxy latence ne voyait PAS : a 8 ms de RTT
    a vide il rendait 100/100.
    """
    note = compute_qoe(rtt_ms=8.0, bloat_ms=250.0)

    assert note is not None
    assert note.grade == "F"
    assert note.severity == "crit"
    assert note.score < 25
    # Le proxy latence, lui, aurait donne la note maximale sur la meme mesure.
    assert qoe_from_rtt(8.0) == 100


def test_le_maillon_faible_fait_la_note() -> None:
    """Aucun bufferbloat, mais 300 ms de latence de base : ce n'est pas 'excellent'.

    Le score compose les DEUX composantes et garde la pire : un chemin
    intrinsequement long reste injouable, meme parfaitement gere.
    """
    note = compute_qoe(rtt_ms=300.0, bloat_ms=1.0)

    assert note is not None
    assert note.grade == "A+"  # le bufferbloat, lui, est irreprochable
    assert note.severity == "crit"  # mais l'experience ne l'est pas
    assert note.score < 20


# ------------------------------------------------------- cas limites exiges
def test_rtt_absent() -> None:
    """Sonde coupee, abonne qui bloque l'ICMP : pas de RTT, donc pas de note."""
    assert compute_qoe(rtt_ms=None, bloat_ms=None) is None


def test_rtt_absent_mais_bufferbloat_connu() -> None:
    """Le bufferbloat seul suffit a conclure : il PORTE deja une latence."""
    note = compute_qoe(rtt_ms=None, bloat_ms=150.0)

    assert note is not None
    assert note.basis == BASIS_LOAD
    assert note.severity == "crit"


def test_pas_de_charge_a_correler_replie_sur_le_proxy_et_le_dit() -> None:
    """Abonne silencieux : on n'invente pas une latence sous charge.

    On retombe sur le proxy latence — c'est tout ce qu'on a — mais la note le
    DIT (``basis="latency"``, pas de note A+..F). Un repli annonce ne se fera
    jamais passer pour une mesure sous charge.
    """
    note = compute_qoe(rtt_ms=20.0, bloat_ms=None)

    assert note is not None
    assert note.basis == BASIS_LATENCY
    assert note.grade is None
    assert note.from_load is False
    assert note.score == qoe_from_rtt(20.0)


def test_un_abonne_silencieux_n_a_pas_de_verdict_donc_pas_de_note_sous_charge() -> None:
    """Bout en bout : ``compute_bufferbloat`` refuse de conclure sans charge, et
    la note qui en decoule est explicitement un repli."""
    # Quatre echantillons, jamais le moindre octet : aucune charge a correler.
    verdict = compute_bufferbloat([(12.0, 0.0)] * 6)
    assert verdict is None

    note = qoe_from_verdict(verdict, rtt_ms=12.0)
    assert note is not None and note.basis == BASIS_LATENCY


# ------------------------------------------------ coherence chiffre/couleur
@pytest.mark.parametrize(
    "bloat_ms", [0.0, 2.0, 5.0, 5.5, 20.0, 30.0, 45.0, 60.0, 80.0, 100.0, 150.0, 200.0, 500.0]
)
def test_la_couleur_du_score_est_celle_de_la_note_de_bufferbloat(bloat_ms: float) -> None:
    """Un seul bareme de couleur, pas deux.

    La severite d'une note issue du bufferbloat est celle de la note A+..F prise
    telle quelle : sans cela, une pastille 'B' verte finirait a cote d'un score
    rouge calcule sur la meme mesure. Les points d'ancrage garantissent en outre
    que le CHIFFRE tombe dans la bande de couleur correspondante.
    """
    note = compute_qoe(rtt_ms=5.0, bloat_ms=bloat_ms)

    assert note is not None
    attendue = GRADE_SEVERITY[grade_for_bloat(bloat_ms)]
    assert note.severity == attendue
    assert qoe_severity(note.score) == attendue


def test_le_score_decroit_avec_le_bufferbloat() -> None:
    scores = [qoe_from_bloat(b) for b in (0.0, 10.0, 50.0, 120.0, 300.0)]
    assert all(
        a is not None and b is not None and a > b for a, b in zip(scores, scores[1:], strict=False)
    )


def test_un_bufferbloat_extreme_tombe_a_zero() -> None:
    assert qoe_from_bloat(5_000.0) == 0.0


# ----------------------------------------------------------------- verdict
def test_note_depuis_un_verdict_de_bufferbloat() -> None:
    """Le raccourci utilise par le depot : idle_ms sert de latence de reference."""
    verdict = BufferbloatVerdict(
        grade="C",
        severity="warn",
        idle_ms=12.0,
        loaded_ms=92.0,
        bloat_ms=80.0,
        samples=20,
        loaded_samples=8,
        load_max_bps=50e6,
    )

    note = qoe_from_verdict(verdict)

    assert note is not None
    assert note.rtt_ms == 12.0
    assert note.bloat_ms == 80.0
    assert note.severity == "warn"
    assert note.basis == BASIS_COMPOSITE


def test_serialisation() -> None:
    note = compute_qoe(rtt_ms=12.345, bloat_ms=80.0)
    assert note is not None
    rendu = note.as_dict()
    assert rendu["grade"] == "C"
    assert rendu["rtt_ms"] == 12.3
    assert rendu["bloat_ms"] == 80.0
    assert rendu["basis"] == BASIS_COMPOSITE
