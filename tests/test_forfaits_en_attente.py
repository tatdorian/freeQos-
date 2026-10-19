"""Un forfait peut PRECEDER l'abonne : il attend sa premiere connexion.

L'interface de gestion de l'operateur -- celle qui alimente le RADIUS -- cree
l'abonne et son forfait avant sa premiere session PPPoE, et sa synchronisation
de depart pousse aussi les abonnes hors ligne. Refuser ces forfaits ("unknown
client") obligeait l'integration a reessayer sans savoir quand.

Ce fichier verrouille l'API (sans base) ; le parcours complet jusqu'a la file
RouterOS est dans ``test_bout_en_bout_api_externe.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.main import register_routes
from app.services.plans import ClientPlanProvider
from tests.test_api import build_container


class ForfaitsMemoire:
    """Double de ClientPlansRepository."""

    def __init__(self) -> None:
        self.lignes: dict[str, dict[str, Any]] = {}

    async def list_all(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.lignes.items()}

    async def get_many(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.lignes.items() if k in logins}

    async def set(self, login: str, **champs: Any) -> dict[str, Any]:
        self.lignes[login] = {"login": login, **champs, "updated_at": datetime.now(tz=UTC)}
        return dict(self.lignes[login])

    async def delete(self, login: str) -> bool:
        return self.lignes.pop(login, None) is not None


class FichesStatiques:
    """Une fiche declaree, pas encore collectee (absente de l'effectif)."""

    def __init__(self) -> None:
        self.plans: dict[str, tuple[Any, Any]] = {}

    async def list_all(self, *, pop_name: str | None = None) -> list[dict[str, Any]]:
        return [{"reference": "mairie", "pop_name": "PoP Test", "address": "10.0.0.5/32"}]

    async def set_plan(self, reference: str, down: Any, up: Any) -> bool:
        self.plans[reference] = (down, up)
        return True


@pytest.fixture
def api(settings: Settings) -> tuple[TestClient, ForfaitsMemoire, FichesStatiques]:
    forfaits = ForfaitsMemoire()
    statiques = FichesStatiques()
    container = build_container(settings, static_clients_repo=statiques)
    container.client_plans_repo = forfaits  # type: ignore[assignment]

    async def effectif() -> list[dict[str, Any]]:
        return [
            {
                "subscriber_id": 1,
                "login": "dupont",
                "kind": "pppoe",
                "pop_name": "PoP Test",
                "address": "10.20.0.10",
                "last_seen": None,
                "plan_down_mbps": 100.0,
                "plan_up_mbps": 20.0,
            }
        ]

    container.repository.plan_roster = effectif  # type: ignore[attr-defined]
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app), forfaits, statiques


def test_un_abonne_jamais_connecte_recoit_son_forfait_en_attente(api) -> None:
    client, forfaits, _ = api
    reponse = client.put("/api/v1/plans/martin", json={"down_mbps": 20, "up_mbps": 5})
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["pending"] is True
    assert corps["enforcement"]["state"] == "en-attente-de-connexion"
    assert (forfaits.lignes["martin"]["down_mbps"], forfaits.lignes["martin"]["up_mbps"]) == (20, 5)

    liste = client.get("/api/v1/plans").json()
    assert [p["login"] for p in liste["pending"]] == ["martin"]
    assert liste["summary"]["pending"] == 1


async def test_le_forfait_en_attente_est_celui_de_la_premiere_connexion(api) -> None:
    """La collecte demande le plan de tout identifiant nouveau : c'est la meme
    table, donc le forfait recu en avance est celui qui s'applique."""
    client, forfaits, _ = api
    client.put("/api/v1/plans/martin", json={"down_mbps": 20, "up_mbps": 5})
    plan = await ClientPlanProvider(forfaits, None).get_plan("martin")
    assert plan is not None and (plan.down_mbps, plan.up_mbps) == (20, 5)


def test_un_abonne_connu_n_est_jamais_en_attente(api) -> None:
    client, forfaits, _ = api
    forfaits.lignes["dupont"] = {"login": "dupont", "down_mbps": 50, "up_mbps": 10, "source": "ui"}
    liste = client.get("/api/v1/plans").json()
    assert liste["pending"] == []


def test_une_fiche_declaree_pas_encore_collectee_garde_son_forfait_dans_sa_fiche(api) -> None:
    client, forfaits, statiques = api
    reponse = client.put("/api/v1/plans/mairie", json={"down_mbps": 200, "up_mbps": 50})
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["pending"] is False
    assert statiques.plans["mairie"] == (200, 50)
    assert "mairie" not in forfaits.lignes


def test_oublier_un_forfait_en_attente(api) -> None:
    client, forfaits, _ = api
    client.put("/api/v1/plans/martni", json={"down_mbps": 20})  # identifiant mal saisi
    reponse = client.delete("/api/v1/plans/martni")
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["pending"] is True
    assert "martni" not in forfaits.lignes
    # Rien a oublier : toujours inconnu.
    assert client.delete("/api/v1/plans/personne").status_code == 404


@pytest.mark.parametrize("login", ["%20martin", "martin%20", "mar%0Atin"])
def test_un_identifiant_mal_forme_est_refuse(api, login: str) -> None:
    client, forfaits, _ = api
    reponse = client.put(f"/api/v1/plans/{login}", json={"down_mbps": 20})
    assert reponse.status_code == 422
    assert forfaits.lignes == {}


def test_un_forfait_sans_debit_reste_refuse(api) -> None:
    client, _, _ = api
    assert client.put("/api/v1/plans/martin", json={}).status_code == 422


def test_avec_les_forfaits_lus_dans_radius_le_forfait_en_attente_est_refuse(
    api, settings: Settings
) -> None:
    """PLAN_PROVIDER=freeradius_sql : a la premiere connexion, le forfait vient du
    RADIUS. Garder celui-ci promettrait ce qui n'arrivera pas : refus explicite."""
    client, forfaits, _ = api
    settings.plan_provider = "freeradius_sql"
    reponse = client.put("/api/v1/plans/martin", json={"down_mbps": 20})
    assert reponse.status_code == 409
    assert "RADIUS" in reponse.json()["detail"]
    assert forfaits.lignes == {}
    # Un abonne deja connu garde le comportement d'avant.
    assert client.put("/api/v1/plans/dupont", json={"down_mbps": 50}).status_code == 200
