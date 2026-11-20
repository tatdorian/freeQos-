"""Clients SANS forfait : chacun recoit SA file a la limite par defaut.

DEMANDE : reperer les clients non configures (session PPPoE ouverte, aucun
forfait) en les bridant, par exemple a 1 kbps. Pas de file « attrape-tout »
en bas de liste : la limite par defaut est posee client par client, et la page
Plans dit exactement qui en a une -- jamais un debit que le routeur n'a pas.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.enforcement.models import format_rate
from app.main import register_routes
from app.services.plans import ClientPlanProvider
from tests.test_api import build_container
from tests.test_forfaits_en_attente import ForfaitsMemoire


class SansFiche:
    async def list_all(self, *, pop_name: str | None = None) -> list[dict[str, Any]]:
        return []


def _client(settings: Settings, roster: list[dict[str, Any]]) -> tuple[TestClient, ForfaitsMemoire]:
    forfaits = ForfaitsMemoire()
    container = build_container(settings, static_clients_repo=SansFiche())
    container.client_plans_repo = forfaits  # type: ignore[assignment]

    async def effectif() -> list[dict[str, Any]]:
        return roster

    container.repository.plan_roster = effectif  # type: ignore[attr-defined]
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), forfaits


def _ligne(login: str, down: Any = None, up: Any = None, source: Any = None) -> dict[str, Any]:
    return {
        "subscriber_id": hash(login) % 1000,
        "login": login,
        "kind": "pppoe",
        "pop_name": "PoP Test",
        "address": "10.20.0.10",
        "last_seen": None,
        "plan_down_mbps": down,
        "plan_up_mbps": up,
        "plan_source": source,
    }


@pytest.mark.parametrize("aux_detectes", [False, True])
def test_la_page_dit_qui_est_sans_forfait_et_s_il_est_bride(
    settings: Settings, aux_detectes: bool
) -> None:
    settings.default_plan_down_mbps = 0.001
    settings.default_plan_up_mbps = 0.001
    settings.default_plan_for_detected_clients = aux_detectes
    roster = [
        _ligne("acme", 50, 10, "ui"),
        # Session vue, jamais de forfait ; quand le reglage est actif, le
        # debit par defaut est recopie sur sa fiche : ce n'est pas un forfait.
        _ligne(
            "inconnu",
            0.001 if aux_detectes else None,
            0.001 if aux_detectes else None,
            "default" if aux_detectes else None,
        ),
    ]
    client, forfaits = _client(settings, roster)
    forfaits.lignes["acme"] = {"login": "acme", "down_mbps": 50, "up_mbps": 10, "source": "ui"}

    corps = client.get("/api/v1/plans").json()
    par_login = {c["login"]: c for c in corps["clients"]}
    assert corps["default"]["detected_clients"] is aux_detectes
    assert par_login["acme"]["no_plan"] is False
    assert par_login["acme"]["down_mbps"] == 50
    assert par_login["inconnu"]["no_plan"] is True
    assert par_login["inconnu"]["default_applied"] is aux_detectes
    # Jamais un debit que le routeur n'a pas.
    assert par_login["inconnu"]["down_mbps"] == (0.001 if aux_detectes else None)
    assert corps["summary"]["no_plan"] == 1
    assert corps["summary"]["default_applied"] == (1 if aux_detectes else 0)


def test_un_client_pousse_sans_debit_prend_la_limite_par_defaut(settings: Settings) -> None:
    settings.default_plan_down_mbps = 0.001
    settings.default_plan_up_mbps = 0.001
    settings.default_plan_for_detected_clients = False
    client, forfaits = _client(settings, [_ligne("vide")])
    forfaits.lignes["vide"] = {"login": "vide", "down_mbps": None, "up_mbps": None, "source": "api"}

    vide = client.get("/api/v1/plans").json()["clients"][0]
    assert vide["no_plan"] is True and vide["default_applied"] is True


async def test_le_fournisseur_donne_1_kbps_aux_sessions_sans_forfait(settings: Settings) -> None:
    """Le debit qui part vers le routeur : 1 kbps = max-limit 1000/1000."""
    settings.default_plan_down_mbps = 0.001
    settings.default_plan_up_mbps = 0.001
    settings.default_plan_for_detected_clients = True
    forfaits = ForfaitsMemoire()
    forfaits.lignes["acme"] = {"login": "acme", "down_mbps": 50, "up_mbps": 10, "source": "ui"}

    plans = await ClientPlanProvider(forfaits, settings).get_plans(["acme", "inconnu"])

    assert (plans["acme"].down_mbps, plans["acme"].up_mbps) == (50, 10)
    assert (plans["inconnu"].down_mbps, plans["inconnu"].up_mbps) == (0.001, 0.001)
    assert format_rate(plans["inconnu"].down_mbps) == "1000"

    settings.default_plan_for_detected_clients = False
    plans = await ClientPlanProvider(forfaits, settings).get_plans(["acme", "inconnu"])
    assert "inconnu" not in plans
