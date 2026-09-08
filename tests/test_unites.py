"""Saisie des debits en kilobits, megabits ou gigabits.

Le Mbps reste l'unite interne unique : melanger les unites en base serait une
fabrique a bugs. La conversion se fait donc a l'entree, et une seule fois.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.shaping import BoostInput, PolicyInput, _en_mbps
from app.enforcement.models import format_rate
from app.enforcement.planner import SubscriberTarget, build_plan, desired_state


# ------------------------------------------------------------- conversion
@pytest.mark.parametrize(
    ("mbps", "kbps", "gbps", "attendu"),
    [
        (10, None, None, 10.0),
        (None, 512, None, 0.512),
        (None, 64, None, 0.064),
        (None, None, 2, 2000.0),
        (None, None, 2.5, 2500.0),
        (None, None, None, None),
    ],
)
def test_conversion_vers_mbps(mbps, kbps, gbps, attendu) -> None:
    assert _en_mbps(mbps, kbps, gbps, "download") == attendu


def test_deux_unites_a_la_fois_refusees() -> None:
    """Ambigu : lequel gagne ? Mieux vaut refuser que deviner."""
    with pytest.raises(ValueError, match="une seule unite"):
        _en_mbps(10, 10000, None, "download")


# ------------------------------------------------------------- surcharge
def test_surcharge_en_kilobits() -> None:
    politique = PolicyInput(
        scope="subscriber", target_key="dupont", max_down_kbps=512, max_up_kbps=128
    )
    assert politique.max_down_mbps == 0.512
    assert politique.max_up_mbps == 0.128


def test_surcharge_en_gigabits() -> None:
    politique = PolicyInput(scope="link", target_key="l1", max_down_gbps=10)
    assert politique.max_down_mbps == 10_000


def test_unites_melangeables_entre_sens() -> None:
    """Rien n'interdit un download en Gbps et un upload en kbps."""
    politique = PolicyInput(scope="link", target_key="l1", max_down_gbps=1, max_up_kbps=512)
    assert politique.max_down_mbps == 1000
    assert politique.max_up_mbps == 0.512


def test_surcharge_deux_unites_meme_sens_refusee() -> None:
    with pytest.raises(ValidationError):
        PolicyInput(scope="link", target_key="l1", max_down_mbps=10, max_down_kbps=10_000)


# ----------------------------------------------------------------- boost
def test_boost_en_kilobits() -> None:
    boost = BoostInput(login="dupont", duration_minutes=30, down_kbps=1500)
    assert boost.down_mbps == 1.5


def test_boost_en_gigabits() -> None:
    boost = BoostInput(login="dupont", duration_minutes=30, down_gbps=1, up_mbps=200)
    assert boost.down_mbps == 1000
    assert boost.up_mbps == 200


def test_boost_deux_unites_refuse() -> None:
    with pytest.raises(ValidationError):
        BoostInput(login="x", duration_minutes=10, down_mbps=100, down_gbps=1)


# ------------------------------------- jusqu'a la commande RouterOS
@pytest.mark.parametrize(
    ("mbps", "bits"),
    [(0.064, "64000"), (0.128, "128000"), (0.512, "512000"), (1.5, "1500000")],
)
def test_les_petits_debits_arrivent_juste(mbps: float, bits: str) -> None:
    """Un abonne bride a 512 kbps ne doit pas se retrouver a 0 ni a 1 Mbps."""
    assert format_rate(mbps) == bits


def test_commande_complete_en_kilobits() -> None:
    _, files, _ = desired_state(
        links=[],
        subscribers=[
            SubscriberTarget(
                login="bride",
                interface="<pppoe-bride>",
                address="10.20.0.10",
                plan_down_mbps=0.512,
                plan_up_mbps=0.128,
            )
        ],
    )
    plan = build_plan(
        "pop", desired_types=[], desired_queues=files, actual_types=[], actual_queues=[]
    )
    assert "max-limit=128000/512000" in plan.actions[0].command


def test_idempotence_sur_les_petits_debits() -> None:
    """RouterOS relit '512k' la ou on a ecrit 512000 : ce n'est pas un changement,
    sinon la file serait reecrite a chaque cycle."""
    _, files, _ = desired_state(
        links=[],
        subscribers=[
            SubscriberTarget(
                login="bride",
                interface="<pppoe-bride>",
                address="10.20.0.10",
                plan_down_mbps=0.512,
                plan_up_mbps=0.128,
            )
        ],
    )
    existante = {
        ".id": "*1",
        "name": files[0].name,
        "target": files[0].target,
        "max-limit": "128k/512k",
        "queue": files[0].queue,
        "comment": "freeqos:managed",
    }
    plan = build_plan(
        "pop",
        desired_types=[],
        desired_queues=files,
        actual_types=[],
        actual_queues=[existante],
    )
    assert plan.is_empty
