"""Le plan appartient au CLIENT : API, page Plans, sinon plan par defaut.

DEMANDE EXPLICITE : "les plans sont definis par client, pas par PoP (qui ne sont
que des points de connexion). Pousses par l'API comme Preseem, plusieurs fois
par jour ; un nouveau client recoit le meme plan par defaut que les autres."
Constate : le fournisseur de demonstration INVENTAIT un plan par login (500/100).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.container import build_plan_provider
from app.services.plans import ClientPlanProvider, default_plan


class Depot:
    def __init__(self, lignes: dict[str, dict[str, Any]]) -> None:
        self.lignes = lignes

    async def get_many(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.lignes.items() if k in logins}


REGLAGES = SimpleNamespace(default_plan_down_mbps=100.0, default_plan_up_mbps=20.0)


async def test_un_nouveau_client_recoit_le_plan_par_defaut() -> None:
    plans = await ClientPlanProvider(Depot({}), REGLAGES).get_plans(["nouveau"])
    assert (plans["nouveau"].down_mbps, plans["nouveau"].up_mbps) == (100.0, 20.0)
    assert plans["nouveau"].source == "default"


async def test_le_plan_pousse_par_l_api_fait_foi() -> None:
    depot = Depot(
        {"dupont": {"down_mbps": 300.0, "up_mbps": 50.0, "source": "api", "service_id": "svc-42"}}
    )
    plan = await ClientPlanProvider(depot, REGLAGES).get_plan("dupont")
    assert plan is not None
    assert (plan.down_mbps, plan.up_mbps, plan.source) == (300.0, 50.0, "api:svc-42")


async def test_un_plan_sans_debit_retombe_sur_le_defaut() -> None:
    depot = Depot({"x": {"down_mbps": None, "up_mbps": None, "source": "api"}})
    plan = await ClientPlanProvider(depot, REGLAGES).get_plan("x")
    assert plan is not None and plan.down_mbps == 100.0


def test_sans_limite_par_defaut_pas_de_plan_invente() -> None:
    assert default_plan(SimpleNamespace(default_plan_down_mbps=0, default_plan_up_mbps=0)) is None


def test_avec_une_base_le_simulateur_n_invente_plus_de_plan() -> None:
    """PLAN_PROVIDER=mock (ancien .env.example) : lu comme "clients" en production."""
    reglages = SimpleNamespace(plan_provider="mock", **vars(REGLAGES))
    assert isinstance(build_plan_provider(reglages, Depot({})), ClientPlanProvider)  # type: ignore[arg-type]
