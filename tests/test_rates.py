"""Derivation des debits : c'est ici que se joue la justesse des mesures."""

from __future__ import annotations

from app.services.rates import RateTracker

MO = 1_000_000


def test_premiere_mesure_ne_produit_pas_de_debit() -> None:
    tracker = RateTracker()
    result = tracker.update("s1", ts=0.0, rx_bytes=0, tx_bytes=0, uptime_s=10)
    assert result.rx_bps is None and result.tx_bps is None
    assert result.reason == "premiere mesure"


def test_debit_calcule_sur_deux_mesures() -> None:
    tracker = RateTracker()
    tracker.update("s1", ts=0.0, rx_bytes=0, tx_bytes=0, uptime_s=10)
    # 12,5 Mo en 10 s = 10 Mbit/s
    result = tracker.update("s1", ts=10.0, rx_bytes=12_500_000, tx_bytes=25_000_000, uptime_s=20)
    assert result.rx_bps == 10_000_000.0
    assert result.tx_bps == 20_000_000.0


def test_reconnexion_detectee_par_uptime_qui_recule() -> None:
    """Le cas critique : sans detection on ecrirait un debit negatif ou un pic."""
    tracker = RateTracker()
    tracker.update("s1", ts=0.0, rx_bytes=900 * MO, tx_bytes=900 * MO, uptime_s=7200)
    result = tracker.update("s1", ts=10.0, rx_bytes=1 * MO, tx_bytes=1 * MO, uptime_s=5)

    assert result.rx_bps is None and result.tx_bps is None
    assert result.reason == "session redemarree"
    assert tracker.resets_detected == 1


def test_reconnexion_detectee_par_compteur_qui_decroit() -> None:
    """Meme sans uptime exploitable, un compteur qui recule signale un reset."""
    tracker = RateTracker()
    tracker.update("s1", ts=0.0, rx_bytes=500 * MO, tx_bytes=500 * MO, uptime_s=None)
    result = tracker.update("s1", ts=10.0, rx_bytes=2 * MO, tx_bytes=2 * MO, uptime_s=None)
    assert result.reason == "session redemarree"
    assert tracker.resets_detected == 1


def test_apres_reset_le_debit_repart_au_cycle_suivant() -> None:
    tracker = RateTracker()
    tracker.update("s1", ts=0.0, rx_bytes=900 * MO, tx_bytes=900 * MO, uptime_s=7200)
    tracker.update("s1", ts=10.0, rx_bytes=0, tx_bytes=0, uptime_s=5)
    result = tracker.update("s1", ts=20.0, rx_bytes=12_500_000, tx_bytes=0, uptime_s=15)
    assert result.rx_bps == 10_000_000.0


def test_debit_aberrant_rejete() -> None:
    tracker = RateTracker(max_plausible_bps=1_000_000_000)
    tracker.update("s1", ts=0.0, rx_bytes=0, tx_bytes=0, uptime_s=10)
    # 1 To en 1 s : impossible sur un dernier km, on refuse d'ecrire.
    result = tracker.update("s1", ts=1.0, rx_bytes=10**12, tx_bytes=0, uptime_s=11)
    assert result.rx_bps is None


def test_intervalle_trop_court_conserve_la_reference() -> None:
    """Deux mesures trop rapprochees : on garde l'ancien point de reference
    plutot que de diviser par un delta bruite."""
    tracker = RateTracker(min_interval_s=1.0)
    tracker.update("s1", ts=0.0, rx_bytes=0, tx_bytes=0, uptime_s=10)
    short = tracker.update("s1", ts=0.2, rx_bytes=1000, tx_bytes=0, uptime_s=10)
    assert short.reason == "intervalle trop court"

    # La reference est toujours ts=0 : 12,5 Mo sur 10 s = 10 Mbit/s.
    later = tracker.update("s1", ts=10.0, rx_bytes=12_500_000, tx_bytes=0, uptime_s=20)
    assert later.rx_bps == 10_000_000.0


def test_compteurs_absents_ne_produisent_pas_de_debit() -> None:
    tracker = RateTracker()
    result = tracker.update("s1", ts=0.0, rx_bytes=None, tx_bytes=None, uptime_s=10)
    assert result.reason == "compteurs absents"


def test_prune_libere_les_sessions_disparues() -> None:
    tracker = RateTracker()
    tracker.update("s1", ts=0.0, rx_bytes=0, tx_bytes=0)
    tracker.update("s2", ts=0.0, rx_bytes=0, tx_bytes=0)
    assert len(tracker) == 2
    assert tracker.prune({"s1"}) == 1
    assert len(tracker) == 1
