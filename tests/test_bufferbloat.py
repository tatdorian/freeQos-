"""Bufferbloat : la latence qui gonfle sous charge."""

from __future__ import annotations

from app.services.bufferbloat import (
    compute_bufferbloat,
    grade_for_bloat,
    summarize,
)


def test_bareme_des_notes() -> None:
    """Le bareme suit l'echelle grand public : A+ imperceptible, F injouable."""
    assert grade_for_bloat(0) == "A+"
    assert grade_for_bloat(5) == "A+"
    assert grade_for_bloat(5.1) == "A"
    assert grade_for_bloat(30) == "A"
    assert grade_for_bloat(59) == "B"
    assert grade_for_bloat(99) == "C"
    assert grade_for_bloat(150) == "D"
    assert grade_for_bloat(250) == "F"


def test_un_lien_propre_reste_plat_sous_charge() -> None:
    """RTT stable meme quand le debit monte : pas de bufferbloat, note A+."""
    echantillons = [
        (9.0, 1_000.0),
        (10.0, 200_000_000.0),
        (9.5, 400_000_000.0),
        (10.5, 500_000_000.0),
        (9.0, 480_000_000.0),
        (10.0, 10_000.0),
    ]
    verdict = compute_bufferbloat(echantillons)
    assert verdict is not None
    assert verdict.grade == "A+"
    assert verdict.bloat_ms <= 5


def test_bufferbloat_franc_est_detecte() -> None:
    """8 ms au repos, 300 ms sous charge : c'est le cas d'ecole, note F."""
    echantillons = [
        (8.0, 1_000.0),
        (9.0, 5_000.0),
        (8.5, 2_000.0),
        (280.0, 500_000_000.0),
        (300.0, 520_000_000.0),
        (310.0, 510_000_000.0),
    ]
    verdict = compute_bufferbloat(echantillons)
    assert verdict is not None
    assert verdict.grade == "F"
    assert verdict.bloat_ms > 200
    assert verdict.idle_ms < 20
    assert verdict.loaded_ms > 250
    assert verdict.loaded_samples >= 2


def test_abonne_silencieux_ne_recoit_pas_de_note() -> None:
    """Sans charge, on ne peut RIEN dire du bufferbloat : None, pas un "A+"."""
    echantillons = [(10.0, 0.0), (11.0, 0.0), (9.0, 0.0), (10.0, 0.0)]
    assert compute_bufferbloat(echantillons) is None


def test_trop_peu_d_echantillons_reste_indetermine() -> None:
    assert compute_bufferbloat([(10.0, 100_000_000.0)]) is None


def test_rtt_manquant_est_ignore() -> None:
    """Un abonne qui bloque l'ICMP laisse des trous : on ne les compte pas."""
    echantillons = [
        (None, 500_000_000.0),
        (8.0, 1_000.0),
        (9.0, 2_000.0),
        (8.5, 3_000.0),
        (250.0, 500_000_000.0),
        (260.0, 520_000_000.0),
    ]
    verdict = compute_bufferbloat(echantillons)
    assert verdict is not None
    # Les six lignes sont passees, mais seules cinq portent un RTT.
    assert verdict.samples == 5


def test_synthese_compte_la_distribution_et_le_pire() -> None:
    bon = compute_bufferbloat(
        [(9.0, 1_000.0), (10.0, 400_000_000.0), (9.5, 450_000_000.0), (10.0, 5_000.0)]
    )
    mauvais = compute_bufferbloat(
        [(8.0, 1_000.0), (9.0, 2_000.0), (300.0, 500_000_000.0), (310.0, 520_000_000.0)]
    )
    assert bon is not None and mauvais is not None
    synthese = summarize([bon, mauvais])
    assert synthese["measured"] == 2
    assert synthese["distribution"]["F"] == 1
    assert synthese["worst_bloat_ms"] == mauvais.as_dict()["bloat_ms"]
