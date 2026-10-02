"""Intelligence abonnes et place restante des sites : les regles, sans base."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.insights import ap_room, classify, summarise

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def ligne(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "subscriber_id": 1,
        "login": "x",
        "kind": "pppoe",
        "pop_name": "PoP",
        "plan_down_mbps": 50.0,
        "plan_up_mbps": 10.0,
        "samples": 1000,
        "capped_samples": 0,
        "avg_down_bps": 2e6,
        "prev_avg_down_bps": 2e6,
        "peak_down_bps": 40e6,
        "last_traffic_at": NOW - timedelta(minutes=5),
    }
    base.update(kw)
    return base


def test_une_mauvaise_experience_est_un_risque_de_depart() -> None:
    r = classify(ligne(), {"score": 35, "grade": "D"}, now=NOW)
    assert r["status"] == "at_risk" and "poor experience" in r["reasons"][0]


def test_un_usage_qui_s_effondre_est_un_risque_de_depart() -> None:
    r = classify(ligne(avg_down_bps=0.2e6, prev_avg_down_bps=3e6), None, now=NOW)
    assert r["status"] == "at_risk"
    assert any("usage down 93%" in x for x in r["reasons"])


def test_un_abonne_devenu_muet_est_un_risque_de_depart() -> None:
    r = classify(ligne(last_traffic_at=NOW - timedelta(days=5), avg_down_bps=0.0), None, now=NOW)
    assert any("no traffic for 5 days" in x for x in r["reasons"])


def test_a_l_etroit_avec_une_bonne_experience_est_pret_a_monter() -> None:
    r = classify(ligne(capped_samples=300), {"score": 90}, now=NOW)
    assert r["status"] == "upgrade" and "30% of the time" in r["reasons"][0]


def test_a_l_etroit_mais_latence_mauvaise_n_est_pas_une_vente() -> None:
    """Le reseau le freine, pas son plan : le lui vendre plus cher ne reglerait rien."""
    r = classify(ligne(capped_samples=300), {"score": 30}, now=NOW)
    assert r["status"] == "at_risk"


def test_un_petit_consommateur_qui_baisse_n_alarme_pas() -> None:
    r = classify(ligne(avg_down_bps=100.0, prev_avg_down_bps=2000.0), None, now=NOW)
    assert r["status"] == "healthy"


def test_le_resume_compte_chaque_statut() -> None:
    xs = [classify(ligne(), None, now=NOW), classify(ligne(), {"score": 10}, now=NOW)]
    assert summarise(xs) == {"at_risk": 1, "upgrade": 0, "healthy": 1}


def test_la_place_restante_se_calcule_sur_la_pointe_reelle() -> None:
    # 100 Mbps, cible 80 ; pointe 40 Mbps pour 20 abonnes = 2 Mbps chacun ; reste 40 -> 20.
    r = ap_room(capacity_mbps=100.0, peak_bps=40e6, subscribers=20, poor_share=0.0)
    assert r["room"] == 20 and r["per_subscriber_mbps"] == 2.0


def test_un_site_deja_degrade_n_a_plus_de_place() -> None:
    r = ap_room(capacity_mbps=100.0, peak_bps=10e6, subscribers=20, poor_share=0.25)
    assert r["room"] == 0 and "25%" in r["reason"]


def test_un_site_deja_a_sa_pointe_n_a_plus_de_place() -> None:
    assert ap_room(capacity_mbps=100.0, peak_bps=90e6, subscribers=20, poor_share=None)["room"] == 0


def test_sans_capacite_on_ne_devine_pas() -> None:
    r = ap_room(capacity_mbps=None, peak_bps=10e6, subscribers=5, poor_share=None)
    assert r["room"] is None and "capacity unknown" in r["reason"]
