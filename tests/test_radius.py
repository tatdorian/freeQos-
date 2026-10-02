"""Fournisseur de plans : simulateur et lecture FreeRADIUS."""

from __future__ import annotations

from app.collectors.radius import MockPlanProvider, plan_from_attributes
from app.models import Plan


async def test_mock_est_stable_pour_un_meme_login() -> None:
    provider = MockPlanProvider()
    first = await provider.get_plan("dupont")
    second = await provider.get_plan("dupont")
    assert first == second


async def test_mock_repartit_les_plans() -> None:
    provider = MockPlanProvider()
    plans = await provider.get_plans([f"abonne{i}" for i in range(60)])
    assert len(plans) == 60
    assert len({(p.down_mbps, p.up_mbps) for p in plans.values()}) > 1


async def test_mock_override_explicite() -> None:
    provider = MockPlanProvider()
    provider.set_plan("vip", Plan(down_mbps=1000, up_mbps=500, source="test"))
    plan = await provider.get_plan("vip")
    assert plan is not None and plan.down_mbps == 1000


def test_plan_depuis_mikrotik_rate_limit() -> None:
    plan = plan_from_attributes({"Mikrotik-Rate-Limit": "20M/200M"}, source="radius:user")
    assert plan is not None
    assert plan.down_mbps == 200.0
    assert plan.up_mbps == 20.0
    assert plan.source == "radius:user"


def test_plan_depuis_attributs_wispr() -> None:
    plan = plan_from_attributes(
        {"WISPr-Bandwidth-Max-Down": "100000000", "WISPr-Bandwidth-Max-Up": "20000000"},
        source="radius:group",
    )
    assert plan is not None
    assert (plan.down_mbps, plan.up_mbps) == (100.0, 20.0)


def test_mikrotik_rate_limit_prioritaire_sur_wispr() -> None:
    plan = plan_from_attributes(
        {
            "Mikrotik-Rate-Limit": "10M/50M",
            "WISPr-Bandwidth-Max-Down": "999000000",
            "WISPr-Bandwidth-Max-Up": "999000000",
        },
        source="radius:user",
    )
    assert plan is not None and plan.down_mbps == 50.0


def test_aucun_attribut_exploitable() -> None:
    assert plan_from_attributes({"Framed-IP-Address": "10.0.0.1"}, source="radius") is None
