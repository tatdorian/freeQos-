"""Coup de debit temporaire.

Un boost sans echeance n'est pas un boost mais une surcharge : la duree est ce
qui le definit, et la faire respecter est la responsabilite du controleur — la
file RouterOS, elle, ne sait rien de l'heure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.enforcement.planner import SubscriberTarget, desired_state

MAINTENANT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def abonne(**kwargs) -> SubscriberTarget:
    base = {
        "login": "dupont",
        "interface": "<pppoe-dupont>",
        "plan_down_mbps": 100.0,
        "plan_up_mbps": 20.0,
    }
    return SubscriberTarget(**{**base, **kwargs})


# --------------------------------------------------------- ordre de priorite
def test_le_boost_prime_sur_le_plan() -> None:
    cible = abonne(boost_down_mbps=500, boost_expires_at=MAINTENANT + timedelta(hours=1))
    assert cible.effective_down_at(MAINTENANT) == 500


def test_le_boost_prime_sur_la_surcharge_permanente() -> None:
    cible = abonne(
        override_down_mbps=250,
        boost_down_mbps=500,
        boost_expires_at=MAINTENANT + timedelta(hours=1),
    )
    assert cible.effective_down_at(MAINTENANT) == 500


def test_apres_echeance_on_revient_a_la_surcharge() -> None:
    """Le boost s'efface, il n'ecrase pas ce qui etait pose avant lui."""
    cible = abonne(
        override_down_mbps=250,
        boost_down_mbps=500,
        boost_expires_at=MAINTENANT + timedelta(hours=1),
    )
    assert cible.effective_down_at(MAINTENANT + timedelta(hours=2)) == 250


def test_apres_echeance_sans_surcharge_on_revient_au_plan() -> None:
    cible = abonne(boost_down_mbps=500, boost_expires_at=MAINTENANT + timedelta(minutes=15))
    assert cible.effective_down_at(MAINTENANT + timedelta(minutes=16)) == 100


def test_boost_sur_un_seul_sens() -> None:
    """Booster le download ne doit pas toucher a l'upload."""
    cible = abonne(boost_down_mbps=500, boost_expires_at=MAINTENANT + timedelta(hours=1))
    assert cible.effective_down_at(MAINTENANT) == 500
    assert cible.effective_up_at(MAINTENANT) == 20


def test_boost_sans_echeance_ignore() -> None:
    """Sans duree ce serait une surcharge deguisee, qui ne s'effacerait jamais."""
    cible = abonne(boost_down_mbps=500, boost_expires_at=None)
    assert cible.boost_active(MAINTENANT) is False
    assert cible.effective_down_at(MAINTENANT) == 100


def test_echeance_sans_debit_ignoree() -> None:
    cible = abonne(boost_expires_at=MAINTENANT + timedelta(hours=1))
    assert cible.boost_active(MAINTENANT) is False


def test_echeance_exacte_compte_comme_expiree() -> None:
    echeance = MAINTENANT + timedelta(hours=1)
    cible = abonne(boost_down_mbps=500, boost_expires_at=echeance)
    assert cible.boost_active(echeance - timedelta(seconds=1)) is True
    assert cible.boost_active(echeance) is False


# ------------------------------------------------------------ etat desire
def test_la_file_porte_le_debit_boostee() -> None:
    _, files = desired_state(
        links=[],
        subscribers=[abonne(boost_down_mbps=500, boost_expires_at=MAINTENANT + timedelta(hours=1))],
        now=MAINTENANT,
    )
    assert files[0].max_limit == "20000000/500000000"


def test_la_file_revient_seule_apres_echeance() -> None:
    """Le meme etat desire, evalue plus tard, redonne le plan : c'est ce qui
    permet au job d'expiration de simplement replanifier."""
    cible = abonne(boost_down_mbps=500, boost_expires_at=MAINTENANT + timedelta(minutes=30))

    _, pendant = desired_state(links=[], subscribers=[cible], now=MAINTENANT)
    _, apres = desired_state(links=[], subscribers=[cible], now=MAINTENANT + timedelta(hours=1))

    assert pendant[0].max_limit == "20000000/500000000"
    assert apres[0].max_limit == "20000000/100000000"
