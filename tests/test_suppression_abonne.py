"""Supprimer un abonne : lui, son historique, et ce qui le ferait revenir."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.main import register_routes
from tests.test_api import build_container


class FauxInventaire:
    def __init__(self) -> None:
        self.fiches = [{"id": 7, "reference": "mairie", "pop_name": "PoP Test"}]
        self.supprimes: list[int] = []

    async def list_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.fiches)

    async def delete(self, client_id: int) -> None:
        self.supprimes.append(client_id)


class FauxTopo:
    def __init__(self) -> None:
        self.politiques = {("subscriber", "dupont")}
        self.boosts: set[tuple[str, str]] = set()

    async def delete_policy(self, scope: str, key: str) -> bool:
        return (scope, key) in self.politiques and not self.politiques.remove((scope, key))

    async def clear_boost(self, scope: str, key: str) -> bool:
        return False


@pytest.fixture
def contexte(settings: Settings) -> tuple[TestClient, Any]:
    container = build_container(settings)
    fiche = {"id": 1, "login": "dupont", "kind": "pppoe", "pop_name": "PoP Test"}
    etat: dict[str, Any] = {"fiche": fiche, "supprime": None, "dernier": datetime.now(tz=UTC)}

    async def get_subscriber(sid: int) -> dict[str, Any] | None:
        return dict(etat["fiche"]) if sid == 1 else None

    async def delete_subscriber(sid: int) -> dict[str, Any]:
        etat["supprime"] = sid
        return {**etat["fiche"], "samples": 42, "last_sample": etat["dernier"]}

    container.repository.get_subscriber = get_subscriber  # type: ignore[method-assign]
    container.repository.delete_subscriber = delete_subscriber  # type: ignore[attr-defined]
    container.topology_repo = FauxTopo()  # type: ignore[assignment]
    container.static_clients_repo = FauxInventaire()  # type: ignore[assignment]

    async def enforce_policy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        etat["plafond_retire"] = kwargs.get("removing")
        return {"state": "file-retiree"}

    container.shaping.enforce_policy = enforce_policy  # type: ignore[method-assign]
    app = FastAPI()
    app.state.settings = settings
    register_routes(app, settings)
    app.dependency_overrides[get_container] = lambda: container
    etat["container"] = container
    return TestClient(app), etat


def test_la_suppression_exige_une_confirmation(contexte: tuple[TestClient, Any]) -> None:
    client, etat = contexte
    assert client.delete("/api/v1/subscribers/1").status_code == 400
    assert etat["supprime"] is None


def test_supprimer_retire_l_historique_la_surcharge_et_le_cache(
    contexte: tuple[TestClient, Any],
) -> None:
    client, etat = contexte
    directory = etat["container"].collection.directory
    directory.subscribers["dupont"] = 1
    corps = client.delete("/api/v1/subscribers/1?confirm=true").json()
    assert etat["supprime"] == 1
    assert corps["samples_deleted"] == 42
    assert corps["override_removed"] is True
    assert etat["plafond_retire"] is True  # le routeur est remis d'accord
    # Le cache de la collecte l'oublie : sinon ses mesures iraient a un id mort.
    assert "dupont" not in directory.subscribers


def test_un_abonne_pppoe_encore_connecte_est_annonce_comme_revenant(
    contexte: tuple[TestClient, Any],
) -> None:
    client, _ = contexte
    assert client.delete("/api/v1/subscribers/1?confirm=true").json()["will_reappear"] is True


def test_un_abonne_parti_ne_revient_pas(contexte: tuple[TestClient, Any]) -> None:
    client, etat = contexte
    etat["dernier"] = datetime.now(tz=UTC) - timedelta(hours=3)
    assert client.delete("/api/v1/subscribers/1?confirm=true").json()["will_reappear"] is False


def test_un_client_statique_quitte_aussi_l_inventaire(contexte: tuple[TestClient, Any]) -> None:
    """Sinon la fiche le recreerait au cycle suivant."""
    client, etat = contexte
    etat["fiche"] = {"id": 1, "login": "mairie", "kind": "static", "pop_name": "PoP Test"}
    corps = client.delete("/api/v1/subscribers/1?confirm=true").json()
    assert corps["static_client_removed"] is True
    assert etat["container"].static_clients_repo.supprimes == [7]
    assert corps["will_reappear"] is False


def test_un_abonne_inconnu_est_un_404(contexte: tuple[TestClient, Any]) -> None:
    client, _ = contexte
    assert client.delete("/api/v1/subscribers/99?confirm=true").status_code == 404
